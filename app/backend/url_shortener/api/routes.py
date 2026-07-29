from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from url_shortener.api.dependencies import (
    get_analytics_service,
    get_anti_abuse_service,
    get_auth_service,
    get_link_service,
    get_pool_service,
)
from url_shortener.api.schemas import CreateLinkRequest, LinkResponse, ProviderResponse, StatsResponse, UserResponse
from url_shortener.core.config import get_settings
from url_shortener.services.analytics import AnalyticsService
from url_shortener.services.anti_abuse import AntiAbuseService
from url_shortener.services.auth import AuthService
from url_shortener.services.links import LinkService
from url_shortener.services.pool import CodePoolService

router = APIRouter()
redirect_counter = Counter("url_shortener_redirect_total", "Redirect responses", ["status"])
create_latency = Histogram("url_shortener_create_seconds", "Short link creation latency")


def to_response(link, stat=None) -> LinkResponse:
    base_url = get_settings().public_base_url.rstrip("/")
    return LinkResponse(
        id=link.id,
        short_code=link.short_code,
        short_url=f"{base_url}/{link.short_code}",
        original_url=link.original_url,
        expires_at=link.expires_at,
        is_active=link.is_active,
        click_count=stat.click_count if stat else 0,
        unique_visitors=stat.unique_visitors if stat else 0,
        last_click_at=stat.last_click_at if stat else None,
    )


@router.post("/api/v1/links", response_model=LinkResponse, status_code=201)
def create_link(
    payload: CreateLinkRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
    link_service: LinkService = Depends(get_link_service),
    anti_abuse: AntiAbuseService = Depends(get_anti_abuse_service),
    auth_service: AuthService = Depends(get_auth_service),
):
    api_client = auth_service.authenticate_api_client(x_api_key)
    anti_abuse.enforce_create_rate_limit(request, x_api_key)
    current_user = auth_service.get_current_user(request)
    payload.owner_user_id = current_user.id if current_user else None
    payload.api_client_id = api_client.id if api_client else None
    with create_latency.time():
        return to_response(link_service.create_link(payload))


@router.get("/api/v1/links", response_model=list[LinkResponse])
def list_links(
    request: Request,
    link_service: LinkService = Depends(get_link_service),
    auth_service: AuthService = Depends(get_auth_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
):
    current_user = auth_service.get_current_user(request)
    if current_user is None:
        return []
    owner_user_id = current_user.id
    links = link_service.list_links(owner_user_id=owner_user_id)
    stats_by_link_id = analytics.get_stats_for_link_ids([link.id for link in links])
    return [to_response(link, stats_by_link_id.get(link.id)) for link in links]


@router.delete("/api/v1/links/{short_code}", status_code=204)
def deactivate_link(
    short_code: str,
    request: Request,
    link_service: LinkService = Depends(get_link_service),
    auth_service: AuthService = Depends(get_auth_service),
):
    current_user = auth_service.require_user(request)
    link_service.deactivate(short_code, owner_user_id=current_user.id)
    return Response(status_code=204)


@router.get("/api/v1/analytics/{short_code}", response_model=StatsResponse)
def get_stats(short_code: str, analytics: AnalyticsService = Depends(get_analytics_service)):
    stat = analytics.get_stats(short_code)
    if stat is None:
        return StatsResponse(link_id="", click_count=0, unique_visitors=0, last_click_at=None)
    return StatsResponse(
        link_id=stat.link_id,
        click_count=stat.click_count,
        unique_visitors=stat.unique_visitors,
        last_click_at=stat.last_click_at,
    )


@router.get("/api/v1/pool/status")
def get_pool_status(pool: CodePoolService = Depends(get_pool_service)):
    return pool.status()


@router.get("/oauth/providers", response_model=list[ProviderResponse])
def oauth_providers(auth_service: AuthService = Depends(get_auth_service)):
    return auth_service.get_provider_statuses()


@router.get("/oauth/login/{provider}")
def oauth_login_redirect(provider: str, auth_service: AuthService = Depends(get_auth_service)):
    return auth_service.begin_login(provider)


@router.post("/oauth/login/{provider}", response_model=UserResponse)
def oauth_login(provider: str, response: Response, auth_service: AuthService = Depends(get_auth_service)):
    user = auth_service.login_with_provider(provider, response)
    return UserResponse(**user.__dict__)


@router.get("/oauth/callback/{provider}")
def oauth_callback(provider: str, request: Request, auth_service: AuthService = Depends(get_auth_service)):
    return auth_service.complete_login(provider, request)


@router.post("/oauth/logout", status_code=204)
def oauth_logout(response: Response, auth_service: AuthService = Depends(get_auth_service)):
    auth_service.logout(response)
    response.status_code = 204
    return response


@router.get("/oauth/me", response_model=UserResponse | None)
def oauth_me(request: Request, auth_service: AuthService = Depends(get_auth_service)):
    user = auth_service.get_current_user(request)
    return UserResponse(**user.__dict__) if user else None


@router.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.api_route("/{short_code}", methods=["GET", "HEAD"])
def redirect(
    short_code: str,
    request: Request,
    link_service: LinkService = Depends(get_link_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
    anti_abuse: AntiAbuseService = Depends(get_anti_abuse_service),
):
    anti_abuse.enforce_redirect_rate_limit(request)
    link = link_service.resolve_link(short_code)
    redirect_counter.labels(status="302").inc()
    try:
        analytics.enqueue_redirect(
            link,
            status_code=302,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        # Analytics is an asynchronous/degradable contour in the C4 design;
        # redirect must stay on the critical path even if metrics storage lags.
        pass
    return RedirectResponse(link.original_url, status_code=302)
