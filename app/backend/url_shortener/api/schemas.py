from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CreateLinkRequest(BaseModel):
    original_url: str
    custom_alias: str | None = Field(default=None, min_length=4, max_length=8)
    expires_at: datetime | None = None
    owner_user_id: str | None = None
    api_client_id: str | None = None

    @field_validator("original_url")
    @classmethod
    def normalize_missing_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("original_url is required")
        if not value.lower().startswith(("http://", "https://")):
            return f"https://{value}"
        return value


class LinkResponse(BaseModel):
    id: str
    short_code: str
    short_url: str
    original_url: str
    expires_at: datetime | None
    is_active: bool
    click_count: int = 0
    unique_visitors: int = 0
    last_click_at: datetime | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    status: str
    providers: list[str]


class ProviderResponse(BaseModel):
    id: str
    label: str
    available: bool
    degraded_message: str | None = None


class ErrorResponse(BaseModel):
    detail: str


class StatsResponse(BaseModel):
    link_id: str
    click_count: int
    unique_visitors: int
    last_click_at: datetime | None
