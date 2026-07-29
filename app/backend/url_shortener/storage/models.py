import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def now_utc() -> datetime:
    return datetime.now(UTC)


class ShortCodeRegistry(Base):
    __tablename__ = "DAT_SHORT_CODE_REGISTRY"

    short_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    is_custom_alias: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_reserved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class PoolGenerationState(Base):
    __tablename__ = "DAT_POOL_GENERATION_STATE"

    generator_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_sequence_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class User(Base):
    __tablename__ = "DAT_USERS"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class UserOAuthAccount(Base):
    __tablename__ = "DAT_USER_OAUTH_ACCOUNTS"
    __table_args__ = (UniqueConstraint("provider", "provider_user_id", name="uq_user_oauth_provider"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("DAT_USERS.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiClient(Base):
    __tablename__ = "DAT_API_CLIENTS"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShortLink(Base):
    __tablename__ = "DAT_SHORT_LINKS"
    __table_args__ = (
        UniqueConstraint("short_code", name="uq_short_links_short_code"),
        UniqueConstraint("custom_alias", name="uq_short_links_custom_alias"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # These references are validated at the application boundary.
    # Physical FK constraints are intentionally omitted because links are sharded by short_code,
    # while identity data lives in the control-plane database.
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    api_client_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    short_code: Mapped[str] = mapped_column(String(8), ForeignKey("DAT_SHORT_CODE_REGISTRY.short_code"), nullable=False)
    custom_alias: Mapped[str | None] = mapped_column(String(8), nullable=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DenyList(Base):
    __tablename__ = "DAT_DENY_LIST"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_value: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RateLimitState(Base):
    __tablename__ = "DAT_RATE_LIMIT_STATE"
    __table_args__ = (UniqueConstraint("scope", "bucket_key", name="uq_rate_limit_scope_bucket"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_key: Mapped[str] = mapped_column(String(255), nullable=False)
    current_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class LinkRedirectEvent(Base):
    __tablename__ = "DAT_LINK_REDIRECT_EVENTS"
    __table_args__ = {"postgresql_partition_by": "RANGE (occurred_at)"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    link_id: Mapped[str] = mapped_column(String(36), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=now_utc, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    referer_domain: Mapped[str | None] = mapped_column(String(255))
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    country_code: Mapped[str | None] = mapped_column(String(2))


class LinkStatsDaily(Base):
    __tablename__ = "DAT_LINK_STATS_DAILY"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stat_date: Mapped[Date] = mapped_column(Date, primary_key=True)
    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_visitors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_click_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
