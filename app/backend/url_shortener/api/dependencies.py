from functools import lru_cache

from url_shortener.core.config import get_settings
from url_shortener.services.analytics import AnalyticsService
from url_shortener.services.anti_abuse import AntiAbuseService
from url_shortener.services.auth import AuthService
from url_shortener.services.links import LinkService
from url_shortener.services.pool import CodePoolService
from url_shortener.storage.cache import get_cache
from url_shortener.storage.database import get_database


@lru_cache
def get_analytics_service() -> AnalyticsService:
    return AnalyticsService(get_database(), get_cache(), get_settings())


@lru_cache
def get_pool_service() -> CodePoolService:
    return CodePoolService(get_database(), get_settings())


def get_link_service() -> LinkService:
    return LinkService(get_database(), get_cache(), get_pool_service(), get_settings(), get_anti_abuse_service())


def get_anti_abuse_service() -> AntiAbuseService:
    return AntiAbuseService(get_database(), get_cache(), get_settings())


def get_auth_service() -> AuthService:
    return AuthService(get_database(), get_cache(), get_settings())
