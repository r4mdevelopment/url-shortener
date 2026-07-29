from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_urlsafe
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select

from url_shortener.core.config import Settings
from url_shortener.storage.cache import Cache
from url_shortener.storage.database import ShardedDatabase
from url_shortener.core.security import sha256_hex
from url_shortener.storage.models import ApiClient, User, UserOAuthAccount, now_utc

SESSION_COOKIE = "shortly_session"
OAUTH_STATE_COOKIE = "shortly_oauth_state"


@dataclass
class AuthUser:
    id: str
    email: str
    display_name: str
    status: str
    providers: list[str]


@dataclass
class ApiClientPrincipal:
    id: str
    client_name: str
    is_active: bool


@dataclass
class OAuthProvider:
    id: str
    label: str
    client_id: str | None
    client_secret: str | None
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str
    token_scheme: str = "Bearer"
    metadata_url: str | None = None
    jwks_uri: str | None = None


class AuthService:
    def __init__(self, db: ShardedDatabase, cache: Cache, settings: Settings):
        self.db = db
        self.cache = cache
        self.settings = settings
        self.serializer = URLSafeSerializer(settings.session_secret, salt="shortly-session")
        self.state_serializer = URLSafeSerializer(settings.session_secret, salt="shortly-oauth-state")
        self.mock_providers = {
            "vk": {"label": "VK", "email": "student@example.com", "display_name": "VK User", "available": True},
            "google": {"label": "Google", "email": "student@example.com", "display_name": "Google User", "available": True},
            "yandex": {"label": "Yandex", "email": "student@example.com", "display_name": "Yandex User", "available": True},
        }

    def get_provider_statuses(self) -> list[dict]:
        if self.settings.oauth_mock_enabled:
            return [
                {
                    "id": provider_id,
                    "label": provider["label"],
                    "available": provider["available"],
                    "degraded_message": None if provider["available"] else "Provider temporarily unavailable.",
                }
                for provider_id, provider in self.mock_providers.items()
            ]

        providers: list[dict] = []
        for provider in self._configured_providers():
            available = bool(provider.client_id and provider.client_secret)
            degraded_message = None if available else "Настройте OAuth-ключи провайдера в переменных окружения."
            if available and provider.metadata_url:
                try:
                    self._get_provider_metadata(provider)
                except HTTPException:
                    available = False
                    degraded_message = "Провайдер сейчас отвечает нестабильно. Попробуйте другой способ входа."
            providers.append(
                {
                    "id": provider.id,
                    "label": provider.label,
                    "available": available,
                    "degraded_message": degraded_message,
                }
            )
        return providers

    def begin_login(self, provider_id: str) -> RedirectResponse:
        provider = self._get_provider(provider_id)
        state = self.state_serializer.dumps(
            {
                "provider": provider.id,
                "issued_at": int(datetime.now(UTC).timestamp()),
                "nonce": token_urlsafe(12),
            }
        )
        response = RedirectResponse(self._build_authorize_url(provider, state), status_code=302)
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            state,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=self.settings.oauth_state_ttl_seconds,
            path="/",
        )
        return response

    def complete_login(self, provider_id: str, request: Request) -> RedirectResponse:
        provider = self._get_provider(provider_id)
        state = request.query_params.get("state")
        code = request.query_params.get("code")
        if not state or not code:
            error = request.query_params.get("error", "oauth_failed")
            return self._redirect_with_error(provider.id, error)

        self._verify_oauth_state(provider.id, state, request.cookies.get(OAUTH_STATE_COOKIE))

        try:
            token = self._exchange_code(provider, code)
            profile = self._fetch_profile(provider, token)
            user = self._upsert_oauth_user(provider.id, profile)
        except HTTPException as exc:
            return self._redirect_with_error(provider.id, exc.detail if isinstance(exc.detail, str) else "oauth_failed")

        response = RedirectResponse("/", status_code=302)
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        self._set_session_cookie(response, user.id)
        return response

    def login_with_provider(self, provider: str, response: Response) -> AuthUser:
        if not self.settings.oauth_mock_enabled:
            raise HTTPException(
                status_code=405,
                detail="Use browser redirect flow at /oauth/login/{provider} for real OAuth providers",
            )

        provider_info = self.mock_providers.get(provider)
        if provider_info is None:
            raise HTTPException(status_code=404, detail="OAuth provider not found")
        if not provider_info["available"]:
            raise HTTPException(status_code=503, detail="Provider temporarily unavailable. Try another provider.")

        provider_user_id = f"{provider}:{provider_info['email']}"
        user = self._find_or_create_user(provider, provider_user_id, provider_info["email"], provider_info["display_name"])
        self._set_session_cookie(response, user.id)
        return self._build_auth_user(user)

    def logout(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")

    def get_current_user(self, request: Request) -> AuthUser | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        try:
            payload = self.serializer.loads(token)
        except BadSignature:
            return None
        user_id = payload.get("user_id")
        if not user_id:
            return None
        found = self._find_user_with_session(user_id=user_id)
        if not found:
            return None
        user, session = found
        try:
            return self._build_auth_user(user, session=session)
        finally:
            session.close()

    def require_user(self, request: Request) -> AuthUser:
        user = self.get_current_user(request)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
        return user

    def authenticate_api_client(self, api_key: str | None) -> ApiClientPrincipal | None:
        if not api_key:
            return None
        api_key_hash = sha256_hex(api_key)
        with self.db.control_session() as session:
            client = session.execute(select(ApiClient).where(ApiClient.api_key_hash == api_key_hash)).scalar_one_or_none()
            if client is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
            if not client.is_active:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API client is inactive")
            client.last_used_at = now_utc()
            return ApiClientPrincipal(id=client.id, client_name=client.client_name, is_active=client.is_active)

    def _configured_providers(self) -> list[OAuthProvider]:
        google_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
        google_metadata = None
        try:
            google_metadata = self._get_provider_metadata(
                OAuthProvider(
                    id="google",
                    label="Google",
                    client_id=self.settings.google_oauth_client_id,
                    client_secret=self.settings.google_oauth_client_secret,
                    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
                    token_url="https://oauth2.googleapis.com/token",
                    userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
                    scope="openid email profile",
                    metadata_url=google_metadata_url,
                )
            )
        except HTTPException:
            google_metadata = {}

        return [
            OAuthProvider(
                id="vk",
                label="VK",
                client_id=self.settings.vk_oauth_client_id,
                client_secret=self.settings.vk_oauth_client_secret,
                authorize_url=self.settings.vk_oauth_authorize_url or "",
                token_url=self.settings.vk_oauth_token_url or "",
                userinfo_url=self.settings.vk_oauth_userinfo_url or "",
                scope="email",
            ),
            OAuthProvider(
                id="google",
                label="Google",
                client_id=self.settings.google_oauth_client_id,
                client_secret=self.settings.google_oauth_client_secret,
                authorize_url=google_metadata.get("authorization_endpoint", "https://accounts.google.com/o/oauth2/v2/auth"),
                token_url=google_metadata.get("token_endpoint", "https://oauth2.googleapis.com/token"),
                userinfo_url=google_metadata.get("userinfo_endpoint", "https://openidconnect.googleapis.com/v1/userinfo"),
                scope="openid email profile",
                metadata_url=google_metadata_url,
                jwks_uri=google_metadata.get("jwks_uri"),
            ),
            OAuthProvider(
                id="yandex",
                label="Yandex",
                client_id=self.settings.yandex_oauth_client_id,
                client_secret=self.settings.yandex_oauth_client_secret,
                authorize_url="https://oauth.yandex.ru/authorize",
                token_url="https://oauth.yandex.ru/token",
                userinfo_url="https://login.yandex.ru/info",
                scope="login:email login:info",
                token_scheme="OAuth",
            ),
        ]

    def _get_provider(self, provider_id: str) -> OAuthProvider:
        provider = next((item for item in self._configured_providers() if item.id == provider_id), None)
        if provider is None:
            raise HTTPException(status_code=404, detail="OAuth provider not found")
        if not provider.client_id or not provider.client_secret or not provider.authorize_url or not provider.token_url or not provider.userinfo_url:
            raise HTTPException(status_code=503, detail=f"{provider.label} OAuth is not configured")
        return provider

    def _build_authorize_url(self, provider: OAuthProvider, state: str) -> str:
        params = {
            "client_id": provider.client_id,
            "redirect_uri": self._callback_url(provider.id),
            "response_type": "code",
            "scope": provider.scope,
            "state": state,
        }
        if provider.id == "google":
            params["access_type"] = "offline"
            params["prompt"] = "select_account"
        return f"{provider.authorize_url}?{urlencode(params)}"

    def _exchange_code(self, provider: OAuthProvider, code: str) -> dict:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "redirect_uri": self._callback_url(provider.id),
        }
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.post(provider.token_url, data=payload, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"{provider.label} OAuth is temporarily unavailable") from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=503, detail=f"{provider.label} token exchange failed")
        data = response.json()
        if "access_token" not in data:
            raise HTTPException(status_code=503, detail=f"{provider.label} did not return an access token")
        return data

    def _fetch_profile(self, provider: OAuthProvider, token_payload: dict) -> dict:
        access_token = token_payload["access_token"]
        headers = {"Authorization": f"{provider.token_scheme} {access_token}", "Accept": "application/json"}
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(provider.userinfo_url, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"{provider.label} userinfo is temporarily unavailable") from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=503, detail=f"{provider.label} userinfo request failed")
        return response.json()

    def _upsert_oauth_user(self, provider_id: str, profile: dict) -> AuthUser:
        provider_user_id = self._extract_provider_user_id(provider_id, profile)
        email = self._extract_email(provider_id, profile, provider_user_id)
        display_name = self._extract_display_name(provider_id, profile, email)
        user = self._find_or_create_user(provider_id, provider_user_id, email, display_name)
        return self._build_auth_user(user)

    def _extract_provider_user_id(self, provider_id: str, profile: dict) -> str:
        candidates = {
            "google": ("sub", "id"),
            "yandex": ("id", "psuid", "uid"),
            "vk": ("user_id", "id", "sub"),
        }.get(provider_id, ("id",))
        for key in candidates:
            value = profile.get(key)
            if value:
                return f"{provider_id}:{value}"
        raise HTTPException(status_code=503, detail=f"{provider_id} profile did not contain a stable user id")

    def _extract_email(self, provider_id: str, profile: dict, provider_user_id: str) -> str:
        for key in ("email", "default_email"):
            value = profile.get(key)
            if value:
                return str(value).lower()
        emails = profile.get("emails")
        if isinstance(emails, list) and emails:
            return str(emails[0]).lower()
        return f"{provider_user_id}@oauth.local"

    def _extract_display_name(self, provider_id: str, profile: dict, fallback_email: str) -> str:
        for key in ("name", "real_name", "display_name", "login"):
            value = profile.get(key)
            if value:
                return str(value)
        first_name = str(profile.get("first_name", "")).strip()
        last_name = str(profile.get("last_name", "")).strip()
        full_name = " ".join(part for part in (first_name, last_name) if part)
        if full_name:
            return full_name
        return fallback_email.split("@", 1)[0]

    def _callback_url(self, provider_id: str) -> str:
        return f"{self.settings.public_base_url.rstrip('/')}/oauth/callback/{provider_id}"

    def _set_session_cookie(self, response: Response, user_id: str) -> None:
        session_token = self.serializer.dumps({"user_id": user_id})
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=60 * 60 * 24 * 7,
            path="/",
        )

    def _verify_oauth_state(self, provider_id: str, state: str, cookie_state: str | None) -> None:
        if not cookie_state or cookie_state != state:
            raise HTTPException(status_code=400, detail="OAuth state mismatch")
        try:
            payload = self.state_serializer.loads(state)
        except BadSignature as exc:
            raise HTTPException(status_code=400, detail="OAuth state is invalid") from exc
        if payload.get("provider") != provider_id:
            raise HTTPException(status_code=400, detail="OAuth state provider mismatch")
        issued_at = int(payload.get("issued_at", 0))
        age = int(datetime.now(UTC).timestamp()) - issued_at
        if age < 0 or age > self.settings.oauth_state_ttl_seconds:
            raise HTTPException(status_code=400, detail="OAuth state expired")

    def _redirect_with_error(self, provider_id: str, message: str) -> RedirectResponse:
        encoded = urlencode({"auth_error": f"{provider_id}:{message}"})
        response = RedirectResponse(f"/?{encoded}", status_code=302)
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return response

    def _get_provider_metadata(self, provider: OAuthProvider) -> dict:
        if not provider.metadata_url:
            return {}
        cache_key = f"oauth:metadata:{provider.id}"
        cached = self.cache.get_json(cache_key)
        if cached:
            return cached
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.get(provider.metadata_url, headers={"Accept": "application/json"})
                response.raise_for_status()
                metadata = response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"{provider.label} metadata fetch failed") from exc
        self.cache.set_json(cache_key, metadata, ttl=3600)
        jwks_uri = metadata.get("jwks_uri")
        if jwks_uri:
            self._cache_provider_jwks(provider.id, jwks_uri)
        return metadata

    def _cache_provider_jwks(self, provider_id: str, jwks_uri: str) -> None:
        cache_key = f"oauth:jwks:{provider_id}"
        if self.cache.get_json(cache_key):
            return
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.get(jwks_uri, headers={"Accept": "application/json"})
                response.raise_for_status()
                self.cache.set_json(cache_key, response.json(), ttl=3600)
        except httpx.HTTPError:
            return

    def _find_or_create_user(self, provider: str, provider_user_id: str, email: str, display_name: str) -> User:
        found_account = self._find_account_with_session(provider, provider_user_id)
        if found_account:
            account, session = found_account
            try:
                user = session.get(User, account.user_id)
                account.last_login_at = now_utc()
                if user and user.display_name != display_name:
                    user.display_name = display_name
                    user.updated_at = now_utc()
                session.commit()
                return user
            finally:
                session.close()

        found_user = self._find_user_with_session(email=email)
        if found_user:
            user, session = found_user
            try:
                account = UserOAuthAccount(
                    user_id=user.id,
                    provider=provider,
                    provider_user_id=provider_user_id,
                    linked_at=now_utc(),
                    last_login_at=now_utc(),
                )
                session.add(account)
                if user.display_name != display_name:
                    user.display_name = display_name
                    user.updated_at = now_utc()
                session.commit()
                return user
            finally:
                session.close()

        with self.db.control_session() as session:
            user = User(email=email, display_name=display_name, status="active")
            session.add(user)
            session.flush()
            account = UserOAuthAccount(
                user_id=user.id,
                provider=provider,
                provider_user_id=provider_user_id,
                linked_at=now_utc(),
                last_login_at=now_utc(),
            )
            session.add(account)
            return user

    def _build_auth_user(self, user: User, session=None) -> AuthUser:
        if session is None:
            found = self._find_user_with_session(user_id=user.id)
            if not found:
                raise HTTPException(status_code=404, detail="User session not found")
            return self._build_auth_user(found[0], session=found[1])
        providers = [
            row.provider
            for row in session.execute(select(UserOAuthAccount).where(UserOAuthAccount.user_id == user.id)).scalars().all()
        ]
        return AuthUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=user.status,
            providers=providers,
        )

    def _find_account_with_session(self, provider: str, provider_user_id: str):
        session = self.db.sessions[0]()
        account = session.execute(
            select(UserOAuthAccount).where(
                UserOAuthAccount.provider == provider,
                UserOAuthAccount.provider_user_id == provider_user_id,
            )
        ).scalar_one_or_none()
        if account:
            return account, session
        session.close()
        return None

    def _find_user_with_session(self, user_id: str | None = None, email: str | None = None):
        session = self.db.sessions[0]()
        if user_id:
            user = session.get(User, user_id)
        else:
            user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user:
            return user, session
        session.close()
        return None
