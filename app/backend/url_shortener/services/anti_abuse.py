from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from url_shortener.core.config import Settings
from url_shortener.storage.cache import Cache
from url_shortener.storage.database import ShardedDatabase
from url_shortener.storage.models import DenyList, RateLimitState


class AntiAbuseService:
    def __init__(self, db: ShardedDatabase, cache: Cache, settings: Settings):
        self.db = db
        self.cache = cache
        self.settings = settings

    def enforce_create_rate_limit(self, request: Request, api_key: str | None) -> None:
        if api_key:
            key = f"rl:api:{api_key}"
            limit = self.settings.api_key_rate_limit_per_second
            ttl = 1
            scope = "api_key_create"
        else:
            host = request.client.host if request.client else "unknown"
            key = f"rl:ip:{host}"
            limit = self.settings.anonymous_rate_limit_per_minute
            ttl = 60
            scope = "ip_create"
        current_count = self.cache.incr_with_ttl(key, ttl)
        self._persist_rate_limit_state(scope=scope, bucket_key=key, current_count=current_count, ttl=ttl)
        if current_count > limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    def enforce_redirect_rate_limit(self, request: Request) -> None:
        host = request.client.host if request.client else "unknown"
        key = f"rl:redirect:ip:{host}"
        ttl = 60
        current_count = self.cache.incr_with_ttl(key, ttl)
        self._persist_rate_limit_state(scope="ip_redirect", bucket_key=key, current_count=current_count, ttl=ttl)
        if current_count > self.settings.redirect_rate_limit_per_minute:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Redirect rate limit exceeded")

    def ensure_url_allowed(self, original_url: str) -> None:
        parsed = urlparse(original_url)
        host = parsed.hostname or ""
        checks = [("domain", host), ("url", original_url)]
        for shard in range(len(self.db.read_sessions)):
            with self.db.read_sessions[shard]() as session:
                for target_type, value in checks:
                    statement = select(DenyList).where(
                        DenyList.target_type == target_type,
                        DenyList.rule_value == value,
                        DenyList.is_active.is_(True),
                    )
                    if session.execute(statement).scalar_one_or_none():
                        raise HTTPException(status_code=400, detail="URL blocked by deny-list")

    def _persist_rate_limit_state(self, scope: str, bucket_key: str, current_count: int, ttl: int) -> None:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl)
        with self.db.control_session() as session:
            statement = select(RateLimitState).where(
                RateLimitState.scope == scope,
                RateLimitState.bucket_key == bucket_key,
            )
            state = session.execute(statement).scalar_one_or_none()
            if state is None:
                try:
                    session.add(
                        RateLimitState(
                            scope=scope,
                            bucket_key=bucket_key,
                            current_count=current_count,
                            window_seconds=ttl,
                            window_started_at=now,
                            expires_at=expires_at,
                            updated_at=now,
                        )
                    )
                    session.flush()
                    return
                except IntegrityError:
                    session.rollback()
                    session.execute(
                        update(RateLimitState)
                        .where(
                            RateLimitState.scope == scope,
                            RateLimitState.bucket_key == bucket_key,
                        )
                        .values(
                            current_count=current_count,
                            window_seconds=ttl,
                            expires_at=expires_at,
                            updated_at=now,
                        )
                    )
                    return
            state_expires_at = self._normalize_timestamp(state.expires_at)
            if state_expires_at <= now:
                state.window_started_at = now
            state.current_count = current_count
            state.window_seconds = ttl
            state.expires_at = expires_at
            state.updated_at = now

    def _normalize_timestamp(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
