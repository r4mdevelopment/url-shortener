import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from url_shortener.api.schemas import CreateLinkRequest
from url_shortener.core.base62 import is_valid_alias
from url_shortener.core.config import Settings
from url_shortener.core.security import sha256_hex
from url_shortener.core.url_validation import validate_public_url
from url_shortener.services.anti_abuse import AntiAbuseService
from url_shortener.services.pool import CodePoolService
from url_shortener.storage.cache import Cache
from url_shortener.storage.database import ShardedDatabase
from url_shortener.storage.models import ShortLink, now_utc


class LinkService:
    _dedupe_locks: dict[str, threading.Lock] = {}
    _dedupe_locks_guard = threading.Lock()

    def __init__(
        self,
        db: ShardedDatabase,
        cache: Cache,
        pool: CodePoolService,
        settings: Settings,
        anti_abuse: AntiAbuseService | None = None,
    ):
        self.db = db
        self.cache = cache
        self.pool = pool
        self.settings = settings
        self.anti_abuse = anti_abuse

    def create_link(self, payload: CreateLinkRequest) -> ShortLink:
        original_url = self._normalize_original_url(str(payload.original_url))
        self._ensure_not_own_short_url(original_url)
        validate_public_url(original_url)
        if self.anti_abuse:
            self.anti_abuse.ensure_url_allowed(original_url)
        expires_at = self._normalize_expires_at(payload.expires_at)
        if payload.custom_alias:
            if not is_valid_alias(payload.custom_alias):
                raise HTTPException(status_code=400, detail="Alias must be 4..8 symbols: latin, digits, '-' or '_'")
            return self._create_with_code(
                payload,
                payload.custom_alias,
                expires_at,
                is_custom_alias=True,
                original_url=original_url,
            )

        with self._lock_for_original_url(original_url):
            existing = self._find_active_by_original_url(original_url)
            if existing:
                return existing

            pooled = self._create_from_pool(payload, expires_at)
            if pooled:
                return pooled
            raise HTTPException(status_code=503, detail="Short-code pool exhausted")

    def _create_from_pool(self, payload: CreateLinkRequest, expires_at: datetime | None) -> ShortLink | None:
        original_url = self._normalize_original_url(str(payload.original_url))
        for factory in self.db.sessions:
            with factory() as session:
                try:
                    code = self.pool.reserve_available_code(session)
                    if code is None:
                        continue
                    link = ShortLink(
                        owner_user_id=payload.owner_user_id,
                        api_client_id=payload.api_client_id,
                        short_code=code,
                        original_url=original_url,
                        original_url_hash=sha256_hex(original_url),
                        expires_at=expires_at,
                    )
                    session.add(link)
                    session.flush()
                    session.commit()
                    self._cache_link(link)
                    return link
                except IntegrityError:
                    session.rollback()
                    continue
        return None

    def resolve_link(self, short_code: str) -> ShortLink:
        cached = self.cache.get_json(f"link:{short_code}")
        if cached:
            expires_at = datetime.fromisoformat(cached["expires_at"]) if cached.get("expires_at") else None
            if self._is_expired(expires_at):
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link expired")
            return ShortLink(
                id=cached["id"],
                short_code=short_code,
                original_url=cached["original_url"],
                original_url_hash=cached["original_url_hash"],
                expires_at=expires_at,
                is_active=cached["is_active"],
            )

        with self.db.read_session_for_key(short_code) as session:
            link = session.execute(select(ShortLink).where(ShortLink.short_code == short_code)).scalar_one_or_none()
            if link is None:
                raise HTTPException(status_code=404, detail="Short link not found")
            if not link.is_active:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link deactivated")
            if self._is_expired(link.expires_at):
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link expired")
            self._cache_link(link)
            return link

    def list_links(self, owner_user_id: str | None = None) -> list[ShortLink]:
        links: list[ShortLink] = []
        for factory in self.db.sessions:
            with factory() as session:
                statement = select(ShortLink).where(ShortLink.is_active.is_(True))
                if owner_user_id:
                    statement = statement.where(ShortLink.owner_user_id == owner_user_id)
                links.extend(session.execute(statement.order_by(ShortLink.created_at.desc())).scalars().all())
        return links

    def deactivate(self, short_code: str, owner_user_id: str) -> None:
        with self.db.session_for_key(short_code) as session:
            link = session.execute(select(ShortLink).where(ShortLink.short_code == short_code)).scalar_one_or_none()
            if link is None:
                raise HTTPException(status_code=404, detail="Short link not found")
            if link.owner_user_id != owner_user_id:
                raise HTTPException(status_code=403, detail="You can only delete your own links")
            link.is_active = False
            link.deactivated_at = now_utc()
            link.updated_at = now_utc()
        self.cache.delete(f"link:{short_code}")

    def _create_with_code(
        self,
        payload: CreateLinkRequest,
        code: str,
        expires_at: datetime | None,
        is_custom_alias: bool,
        original_url: str | None = None,
    ) -> ShortLink:
        original_url = original_url or self._normalize_original_url(str(payload.original_url))
        with self.db.session_for_key(code) as session:
            try:
                self.pool.reserve_specific_code(session, code, is_custom_alias=is_custom_alias)
                link = ShortLink(
                    owner_user_id=payload.owner_user_id,
                    api_client_id=payload.api_client_id,
                    short_code=code,
                    custom_alias=code if is_custom_alias else None,
                    original_url=original_url,
                    original_url_hash=sha256_hex(original_url),
                    expires_at=expires_at,
                )
                session.add(link)
                session.flush()
                self._cache_link(link)
                return link
            except IntegrityError as exc:
                if is_custom_alias:
                    raise HTTPException(status_code=409, detail="Alias already exists") from exc
                raise

    def _normalize_expires_at(self, expires_at: datetime | None) -> datetime:
        now = datetime.now(UTC)
        max_expires_at = now + timedelta(days=self.settings.max_ttl_days)
        if expires_at is None:
            return now + timedelta(days=self.settings.default_ttl_days)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            raise HTTPException(status_code=400, detail="expires_at must be in the future")
        if expires_at > max_expires_at:
            raise HTTPException(status_code=400, detail="TTL must not exceed 30 days")
        return expires_at

    def _normalize_original_url(self, original_url: str) -> str:
        parsed = urlparse(original_url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise HTTPException(status_code=400, detail="URL must use http or https")
        host = (parsed.hostname or "").lower()
        if not host:
            raise HTTPException(status_code=400, detail="original_url must contain host")
        port = f":{parsed.port}" if parsed.port and not self._is_default_port(scheme, parsed.port) else ""
        path = parsed.path or ""
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunparse((scheme, f"{host}{port}", path, "", query, ""))

    def _lock_for_original_url(self, original_url: str) -> threading.Lock:
        key = sha256_hex(original_url)
        with self._dedupe_locks_guard:
            lock = self._dedupe_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._dedupe_locks[key] = lock
            return lock

    def _is_default_port(self, scheme: str, port: int) -> bool:
        return (scheme == "http" and port == 80) or (scheme == "https" and port == 443)

    def _ensure_not_own_short_url(self, original_url: str) -> None:
        original = urlparse(original_url)
        public = urlparse(self.settings.public_base_url)
        same_host = (original.hostname or "").lower() == (public.hostname or "").lower()
        same_port = (original.port or self._default_port(original.scheme)) == (public.port or self._default_port(public.scheme))
        if same_host and same_port:
            raise HTTPException(status_code=400, detail="Already shortened URLs cannot be shortened again")

    def _default_port(self, scheme: str) -> int | None:
        return {"http": 80, "https": 443}.get(scheme)

    def _find_active_by_original_url(self, original_url: str) -> ShortLink | None:
        original_url_hash = sha256_hex(original_url)
        for factory in self.db.sessions:
            with factory() as session:
                link = session.execute(
                    select(ShortLink).where(
                        ShortLink.original_url_hash == original_url_hash,
                        ShortLink.is_active.is_(True),
                        ShortLink.custom_alias.is_(None),
                    )
                ).scalars().first()
                if link is None:
                    continue
                if self._is_expired(link.expires_at):
                    continue
                self._cache_link(link)
                return link
        return None

    def _is_expired(self, expires_at: datetime | None) -> bool:
        if expires_at is None:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= now_utc()

    def _cache_link(self, link: ShortLink) -> None:
        self.cache.set_json(
            f"link:{link.short_code}",
            {
                "id": link.id,
                "original_url": link.original_url,
                "original_url_hash": link.original_url_hash,
                "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                "is_active": link.is_active,
            },
            ttl=self.settings.cache_ttl_seconds,
        )
