from functools import lru_cache

from pydantic import Field, model_validator, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "URL Shortener"
    public_base_url: str = "http://127.0.0.1:8000"
    database_urls: list[str] | str = Field(
        default_factory=lambda: ["sqlite:///./url_shortener_shard_0.db"],
        validation_alias="DATABASE_URLS",
    )
    database_replica_urls: list[str] | str | None = Field(default=None, validation_alias="DATABASE_REPLICA_URLS")
    redis_url: str | None = "redis://localhost:6379/0"
    session_secret: str = "dev-session-secret"
    cache_ttl_seconds: int = 3600
    max_ttl_days: int = 30
    default_ttl_days: int = 30
    anonymous_rate_limit_per_minute: int = 60
    redirect_rate_limit_per_minute: int = 300
    api_key_rate_limit_per_second: int = 10
    pool_min_available_codes: int = 150_000_000
    pool_low_watermark_codes: int = 120_000_000
    pool_seed_batch_size: int = 100_000
    pool_refill_check_interval: int = 100
    pool_background_refill_enabled: bool = True
    short_code_length: int = 8
    analytics_queue_name: str = "analytics:redirect-events"
    analytics_consumer_block_seconds: int = 1
    run_analytics_worker_in_api: bool = True
    oauth_mock_enabled: bool = False
    oauth_state_ttl_seconds: int = 300
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    yandex_oauth_client_id: str | None = None
    yandex_oauth_client_secret: str | None = None
    vk_oauth_client_id: str | None = None
    vk_oauth_client_secret: str | None = None
    vk_oauth_authorize_url: str | None = None
    vk_oauth_token_url: str | None = None
    vk_oauth_userinfo_url: str | None = None

    @field_validator("database_urls", mode="before")
    @classmethod
    def parse_database_urls(cls, value):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("database_replica_urls", mode="before")
    @classmethod
    def parse_database_replica_urls(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def validate_pool_settings(self):
        if self.pool_low_watermark_codes > self.pool_min_available_codes:
            raise ValueError("POOL_LOW_WATERMARK_CODES must be less than or equal to POOL_MIN_AVAILABLE_CODES")
        if self.pool_seed_batch_size < 1:
            raise ValueError("POOL_SEED_BATCH_SIZE must be positive")
        if self.pool_refill_check_interval < 1:
            raise ValueError("POOL_REFILL_CHECK_INTERVAL must be positive")
        if self.analytics_consumer_block_seconds < 1:
            raise ValueError("ANALYTICS_CONSUMER_BLOCK_SECONDS must be positive")
        if self.database_replica_urls and len(self.database_replica_urls) != len(self.database_urls):
            raise ValueError("DATABASE_REPLICA_URLS must contain one replica URL per primary shard")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
