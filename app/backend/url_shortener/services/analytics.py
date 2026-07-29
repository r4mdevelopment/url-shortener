import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from url_shortener.core.config import Settings
from url_shortener.core.security import sha256_hex
from url_shortener.storage.cache import Cache
from url_shortener.storage.database import ShardedDatabase
from url_shortener.storage.models import LinkRedirectEvent, LinkStatsDaily, ShortLink


@dataclass
class RedirectAnalyticsEvent:
    link_id: str
    short_code: str
    status_code: int
    occurred_at: str
    ip_hash: str
    user_agent_hash: str


class AnalyticsService:
    def __init__(self, db: ShardedDatabase, cache: Cache, settings: Settings):
        self.db = db
        self.cache = cache
        self.settings = settings
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def enqueue_redirect(self, link: ShortLink, status_code: int, ip: str | None, user_agent: str | None) -> None:
        occurred_at = datetime.now(UTC)
        event = RedirectAnalyticsEvent(
            link_id=link.id,
            short_code=link.short_code,
            status_code=status_code,
            occurred_at=occurred_at.isoformat(),
            ip_hash=sha256_hex(ip or "unknown"),
            user_agent_hash=sha256_hex(user_agent or "unknown"),
        )
        self.cache.push_json_queue(self.settings.analytics_queue_name, asdict(event))

    def start_worker(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._worker_stop.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="analytics-worker", daemon=True)
        self._worker_thread.start()

    def stop_worker(self) -> None:
        self._worker_stop.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=self.settings.analytics_consumer_block_seconds + 1)

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            payload = self.cache.pop_json_queue(
                self.settings.analytics_queue_name,
                timeout=self.settings.analytics_consumer_block_seconds,
            )
            if payload is None:
                continue
            try:
                self._persist_redirect_event(RedirectAnalyticsEvent(**payload))
            except Exception:
                self.cache.push_json_queue(self.settings.analytics_queue_name, payload)
                time.sleep(0.2)

    def _persist_redirect_event(self, event_payload: RedirectAnalyticsEvent) -> None:
        with self.db.session_for_key(event_payload.short_code) as session:
            occurred_at = datetime.fromisoformat(event_payload.occurred_at)
            start_of_day = occurred_at.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = occurred_at.replace(hour=23, minute=59, second=59, microsecond=999999)
            unique_visitor_exists = session.execute(
                select(LinkRedirectEvent.id)
                .where(
                    LinkRedirectEvent.link_id == event_payload.link_id,
                    LinkRedirectEvent.ip_hash == event_payload.ip_hash,
                    LinkRedirectEvent.user_agent_hash == event_payload.user_agent_hash,
                    LinkRedirectEvent.occurred_at >= start_of_day,
                    LinkRedirectEvent.occurred_at <= end_of_day,
                )
                .limit(1)
            ).scalar_one_or_none()
            event = LinkRedirectEvent(
                link_id=event_payload.link_id,
                status_code=event_payload.status_code,
                ip_hash=event_payload.ip_hash,
                user_agent_hash=event_payload.user_agent_hash,
                occurred_at=occurred_at,
            )
            session.add(event)
            stat_date = occurred_at.date()
            stat = session.get(LinkStatsDaily, (event_payload.link_id, stat_date))
            if stat is None:
                stat = LinkStatsDaily(link_id=event_payload.link_id, stat_date=stat_date, click_count=0, unique_visitors=0)
                session.add(stat)
            stat.click_count += 1
            if unique_visitor_exists is None:
                stat.unique_visitors += 1
            stat.last_click_at = occurred_at
            stat.updated_at = occurred_at

    def get_stats(self, short_code: str) -> LinkStatsDaily | None:
        with self.db.read_session_for_key(short_code) as session:
            link = session.execute(select(ShortLink).where(ShortLink.short_code == short_code)).scalar_one_or_none()
            if not link:
                return None
            return session.execute(select(LinkStatsDaily).where(LinkStatsDaily.link_id == link.id)).scalar_one_or_none()

    def get_stats_for_link_ids(self, link_ids: list[str]) -> dict[str, LinkStatsDaily]:
        if not link_ids:
            return {}
        stats: dict[str, LinkStatsDaily] = {}
        for factory in self.db.read_sessions:
            with factory() as session:
                for stat in session.execute(select(LinkStatsDaily).where(LinkStatsDaily.link_id.in_(link_ids))).scalars().all():
                    current = stats.get(stat.link_id)
                    if current is None:
                        stats[stat.link_id] = stat
                        continue
                    current.click_count += stat.click_count
                    current.unique_visitors += stat.unique_visitors
                    if stat.last_click_at and (current.last_click_at is None or stat.last_click_at > current.last_click_at):
                        current.last_click_at = stat.last_click_at
        return stats
