import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from url_shortener.api.dependencies import get_analytics_service, get_pool_service
from url_shortener.core.config import get_settings
from url_shortener.core.security import sha256_hex
from url_shortener.main import create_app
from url_shortener.storage import cache as cache_module
from url_shortener.storage import database as database_module
from url_shortener.storage.database import get_database
from url_shortener.storage.models import ApiClient, RateLimitState, ShortLink


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    os.environ["DATABASE_URLS"] = f"sqlite:///{tmp_path / 'shard0.db'},sqlite:///{tmp_path / 'shard1.db'}"
    os.environ["REDIS_URL"] = ""
    os.environ["PUBLIC_BASE_URL"] = "http://testserver"
    os.environ["ANONYMOUS_RATE_LIMIT_PER_MINUTE"] = "1000"
    os.environ["REDIRECT_RATE_LIMIT_PER_MINUTE"] = "1000"
    os.environ["POOL_MIN_AVAILABLE_CODES"] = "10"
    os.environ["POOL_LOW_WATERMARK_CODES"] = "5"
    os.environ["POOL_SEED_BATCH_SIZE"] = "10"
    os.environ["POOL_REFILL_CHECK_INTERVAL"] = "1"
    os.environ["OAUTH_MOCK_ENABLED"] = "1"
    get_settings.cache_clear()
    get_analytics_service.cache_clear()
    get_pool_service.cache_clear()
    settings = get_settings()
    database_module.reset_database_for_tests(settings)
    cache_module._cache = cache_module.Cache(settings)
    app = create_app()
    with TestClient(app) as client:
        yield client


def test_create_and_redirect(client: TestClient):
    response = client.post("/api/v1/links", json={"original_url": "example.com/path"})
    assert response.status_code == 201
    code = response.json()["short_code"]

    redirect = client.get(f"/{code}", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "https://example.com/path"


def test_ttl_more_than_30_days_rejected(client: TestClient):
    response = client.post(
        "/api/v1/links",
        json={"original_url": "https://example.com", "expires_at": "2027-01-01T00:00:00Z"},
    )
    assert response.status_code == 400
    assert "30 days" in response.json()["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "not-a-link",
        "example",
        "http://localhost:8080/admin",
        "http://127.0.0.1/private",
        "ftp://example.com/file",
        "https://bad_domain.com",
        "https://example..com",
    ],
)
def test_invalid_original_url_rejected(client: TestClient, url: str):
    response = client.post("/api/v1/links", json={"original_url": url})

    assert response.status_code == 400


def test_repeated_same_original_url_returns_existing_code(client: TestClient):
    first = client.post("/api/v1/links", json={"original_url": "music.hotterinc.ru"}).json()
    second = client.post("/api/v1/links", json={"original_url": "https://music.hotterinc.ru"}).json()

    assert first["short_code"] == second["short_code"]


def test_same_original_url_can_have_many_aliases(client: TestClient):
    first = client.post(
        "/api/v1/links",
        json={"original_url": "vk.com", "custom_alias": "zxczxc"},
    ).json()
    second = client.post(
        "/api/v1/links",
        json={"original_url": "https://vk.com", "custom_alias": "vk2026"},
    ).json()
    plain = client.post("/api/v1/links", json={"original_url": "vk.com"}).json()

    assert first["short_code"] == "zxczxc"
    assert second["short_code"] == "vk2026"
    assert plain["short_code"] not in {"zxczxc", "vk2026"}


def test_concurrent_same_original_url_returns_one_code(client: TestClient):
    def create():
        return client.post("/api/v1/links", json={"original_url": "https://example.com/same"}).json()["short_code"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        codes = list(executor.map(lambda _: create(), range(8)))

    assert len(set(codes)) == 1


def test_pool_status_exposes_available_codes(client: TestClient):
    response = client.get("/api/v1/pool/status")

    assert response.status_code == 200
    assert response.json()["target_available"] == 10
    assert response.json()["low_watermark"] == 5
    assert response.json()["available"] >= 0


def test_oauth_login_and_private_cabinet(client: TestClient):
    login = client.post("/oauth/login/vk")
    assert login.status_code == 200
    assert login.json()["providers"] == ["vk"]

    created = client.post("/api/v1/links", json={"original_url": "https://example.com/private"})
    assert created.status_code == 201

    listed = client.get("/api/v1/links")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["original_url"] == "https://example.com/private"

    me = client.get("/oauth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "student@example.com"


def test_second_provider_links_to_same_account(client: TestClient):
    first = client.post("/oauth/login/google")
    assert first.status_code == 200
    second = client.post("/oauth/login/yandex")
    assert second.status_code == 200
    assert "google" in second.json()["providers"]
    assert "yandex" in second.json()["providers"]


def test_rejects_shortening_own_short_url(client: TestClient):
    created = client.post("/api/v1/links", json={"original_url": "https://example.com/original"}).json()

    response = client.post("/api/v1/links", json={"original_url": created["short_url"]})

    assert response.status_code == 400
    assert "cannot be shortened again" in response.json()["detail"]


def test_concurrent_same_alias_conflicts(client: TestClient):
    def create():
        return client.post(
            "/api/v1/links",
            json={"original_url": "https://example.com/alias", "custom_alias": "promo26"},
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as executor:
        statuses = list(executor.map(lambda _: create(), range(4)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 3


def test_delete_requires_authenticated_owner(client: TestClient):
    created = client.post("/api/v1/links", json={"original_url": "https://example.com/public"}).json()

    anonymous_delete = client.delete(f"/api/v1/links/{created['short_code']}")
    assert anonymous_delete.status_code == 401

    login = client.post("/oauth/login/vk")
    assert login.status_code == 200

    owned = client.post("/api/v1/links", json={"original_url": "https://example.com/private-delete"}).json()
    listed_before = client.get("/api/v1/links")
    assert listed_before.status_code == 200
    assert [item["short_code"] for item in listed_before.json()] == [owned["short_code"]]

    delete_owned = client.delete(f"/api/v1/links/{owned['short_code']}")
    assert delete_owned.status_code == 204

    listed_after = client.get("/api/v1/links")
    assert listed_after.status_code == 200
    assert listed_after.json() == []


def test_cabinet_links_include_click_count(client: TestClient):
    login = client.post("/oauth/login/vk")
    assert login.status_code == 200

    created = client.post("/api/v1/links", json={"original_url": "https://example.com/stats"}).json()
    redirect = client.get(f"/{created['short_code']}", follow_redirects=False)
    assert redirect.status_code == 302

    deadline = time.time() + 2
    while True:
        listed = client.get("/api/v1/links")
        assert listed.status_code == 200
        if listed.json()[0]["click_count"] == 1:
            break
        if time.time() >= deadline:
            raise AssertionError("Analytics worker did not update click count in time")
        time.sleep(0.05)


def test_api_key_authenticates_integrator_and_links_api_client(client: TestClient):
    raw_api_key = "integration-secret"
    with get_database().control_session() as session:
        api_client = ApiClient(client_name="Partner A", api_key_hash=sha256_hex(raw_api_key), is_active=True)
        session.add(api_client)
        session.flush()
        api_client_id = api_client.id

    response = client.post(
        "/api/v1/links",
        json={"original_url": "https://example.com/integration"},
        headers={"x-api-key": raw_api_key},
    )
    assert response.status_code == 201

    short_code = response.json()["short_code"]
    with get_database().session_for_key(short_code) as session:
        created = session.execute(select(ShortLink).where(ShortLink.short_code == short_code)).scalar_one()
        assert created.api_client_id == api_client_id

    with get_database().control_session() as session:
        stored_client = session.get(ApiClient, api_client_id)
        assert stored_client is not None
        assert stored_client.last_used_at is not None


def test_invalid_api_key_is_rejected(client: TestClient):
    response = client.post(
        "/api/v1/links",
        json={"original_url": "https://example.com/reject"},
        headers={"x-api-key": "wrong-key"},
    )

    assert response.status_code == 401


def test_redirect_rate_limit_is_enforced(client: TestClient):
    os.environ["REDIRECT_RATE_LIMIT_PER_MINUTE"] = "2"
    get_settings.cache_clear()
    get_analytics_service.cache_clear()
    get_pool_service.cache_clear()

    response = client.post("/api/v1/links", json={"original_url": "https://example.com/hot"})
    assert response.status_code == 201
    short_code = response.json()["short_code"]

    first = client.get(f"/{short_code}", follow_redirects=False)
    second = client.get(f"/{short_code}", follow_redirects=False)
    third = client.get(f"/{short_code}", follow_redirects=False)

    assert first.status_code == 302
    assert second.status_code == 302
    assert third.status_code == 429

    with get_database().control_session() as session:
        state = session.execute(
            select(RateLimitState).where(
                RateLimitState.scope == "ip_redirect",
                RateLimitState.bucket_key == "rl:redirect:ip:testclient",
            )
        ).scalar_one_or_none()
        assert state is not None
        assert state.current_count == 3
        assert state.window_seconds == 60


def test_unique_visitors_are_deduplicated_per_day(client: TestClient):
    response = client.post("/api/v1/links", json={"original_url": "https://example.com/visitors"})
    assert response.status_code == 201
    short_code = response.json()["short_code"]

    first = client.get(f"/{short_code}", follow_redirects=False, headers={"user-agent": "Browser-A"})
    second = client.get(f"/{short_code}", follow_redirects=False, headers={"user-agent": "Browser-A"})
    third = client.get(f"/{short_code}", follow_redirects=False, headers={"user-agent": "Browser-B"})

    assert first.status_code == 302
    assert second.status_code == 302
    assert third.status_code == 302

    deadline = time.time() + 2
    while True:
        stats = client.get(f"/api/v1/analytics/{short_code}")
        assert stats.status_code == 200
        payload = stats.json()
        if payload["click_count"] == 3:
            assert payload["unique_visitors"] == 2
            break
        if time.time() >= deadline:
            raise AssertionError("Analytics worker did not update unique visitor stats in time")
        time.sleep(0.05)
