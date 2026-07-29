import threading
from datetime import UTC, datetime

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from url_shortener.core.base62 import encode_number
from url_shortener.core.config import Settings
from url_shortener.storage.database import ShardedDatabase
from url_shortener.storage.models import PoolGenerationState, ShortCodeRegistry, now_utc

POOL_GENERATOR_NAME = "default"


class CodePoolService:
    _sql_batch_size = 10_000

    def __init__(self, db: ShardedDatabase, settings: Settings):
        self.db = db
        self.settings = settings
        self._background_refill_lock = threading.Lock()
        self._background_refill_thread: threading.Thread | None = None
        self._consumed_since_last_check = 0
        self._consumed_guard = threading.Lock()

    def initialize_runtime(self) -> None:
        self._ensure_generation_state()
        available = self._count_available()
        if available < self.settings.pool_low_watermark_codes:
            self._fill_missing_codes(
                min(
                    self.settings.pool_seed_batch_size,
                    self.settings.pool_low_watermark_codes - available,
                )
            )
        if self.settings.pool_background_refill_enabled:
            self.ensure_capacity_async(force_check=True)

    def bootstrap_target_pool(self, target_available: int | None = None) -> dict[str, int | bool]:
        target = target_available or self.settings.pool_min_available_codes
        inserted_total = 0
        while True:
            available = self._count_available()
            if available >= target:
                break
            inserted_total += self._fill_missing_codes(target - available)
            if inserted_total == 0 and self._count_available() < target:
                raise RuntimeError("Unable to generate additional short codes for the pool")
        status = self.status()
        status["inserted_this_run"] = inserted_total
        return status

    def reserve_specific_code(self, session: Session, code: str, is_custom_alias: bool) -> str:
        now = datetime.now(UTC)
        registry = session.get(ShortCodeRegistry, code)
        if registry and (registry.is_reserved or registry.is_used):
            raise IntegrityError("code is already reserved", None, None)
        if registry is None:
            registry = ShortCodeRegistry(short_code=code, is_custom_alias=is_custom_alias)
            session.add(registry)
        registry.is_reserved = True
        registry.is_used = True
        registry.reserved_at = now
        registry.used_at = now
        session.flush()
        return code

    def reserve_available_code(self, session: Session) -> str | None:
        registry = session.execute(
            select(ShortCodeRegistry)
            .where(ShortCodeRegistry.is_reserved.is_(False), ShortCodeRegistry.is_used.is_(False))
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if registry is None:
            return None
        now = datetime.now(UTC)
        registry.is_reserved = True
        registry.is_used = True
        registry.reserved_at = now
        registry.used_at = now
        session.flush()
        self._mark_code_consumed()
        return registry.short_code

    def status(self) -> dict[str, int | bool]:
        available = self._count_available()
        reserved = 0
        used = 0
        total = 0
        for factory in self.db.sessions:
            with factory() as session:
                total += session.scalar(select(func.count()).select_from(ShortCodeRegistry)) or 0
                reserved += (
                    session.scalar(
                        select(func.count())
                        .select_from(ShortCodeRegistry)
                        .where(ShortCodeRegistry.is_reserved.is_(True), ShortCodeRegistry.is_used.is_(False))
                    )
                    or 0
                )
                used += (
                    session.scalar(
                        select(func.count())
                        .select_from(ShortCodeRegistry)
                        .where(ShortCodeRegistry.is_used.is_(True))
                    )
                    or 0
                )
        return {
            "available": available,
            "reserved": reserved,
            "used": used,
            "total": total,
            "target_available": self.settings.pool_min_available_codes,
            "low_watermark": self.settings.pool_low_watermark_codes,
            "refill_batch_size": self.settings.pool_seed_batch_size,
            "below_target": available < self.settings.pool_min_available_codes,
            "below_low_watermark": available < self.settings.pool_low_watermark_codes,
            "background_refill_running": bool(self._background_refill_thread and self._background_refill_thread.is_alive()),
        }

    def ensure_capacity_async(self, force_check: bool = False) -> None:
        if not force_check and not self._needs_capacity_check():
            return
        if self._count_available() >= self.settings.pool_low_watermark_codes:
            return
        if self._background_refill_thread and self._background_refill_thread.is_alive():
            return
        with self._background_refill_lock:
            if self._background_refill_thread and self._background_refill_thread.is_alive():
                return
            self._background_refill_thread = threading.Thread(
                target=self._background_refill_worker,
                name="short-code-pool-refill",
                daemon=True,
            )
            self._background_refill_thread.start()

    def _insert_specific_available_code(self, session: Session, code: str) -> None:
        if session.get(ShortCodeRegistry, code) is None:
            session.add(ShortCodeRegistry(short_code=code))

    def _background_refill_worker(self) -> None:
        while self._count_available() < self.settings.pool_min_available_codes:
            inserted = self._fill_missing_codes(
                min(
                    self.settings.pool_seed_batch_size,
                    self.settings.pool_min_available_codes - self._count_available(),
                )
            )
            if inserted == 0:
                return

    def _fill_missing_codes(self, missing: int) -> int:
        target_batch_size = max(1, min(missing, self.settings.pool_seed_batch_size))
        generated_codes = self._claim_sequence_codes(target_batch_size)
        by_shard: dict[int, list[str]] = {index: [] for index in range(len(self.db.sessions))}
        for code in generated_codes:
            by_shard[self.db.shard_index(code)].append(code)

        inserted_total = 0
        for index, codes in by_shard.items():
            if not codes:
                continue
            with self.db.sessions[index]() as session:
                existing_codes: set[str] = set()
                for code_chunk in self._chunked(codes, self._sql_batch_size):
                    existing_codes.update(
                        session.execute(
                            select(ShortCodeRegistry.short_code).where(ShortCodeRegistry.short_code.in_(code_chunk))
                        ).scalars()
                    )
                rows = [{"short_code": code} for code in codes if code not in existing_codes]
                for row_chunk in self._chunked(rows, self._sql_batch_size):
                    if not row_chunk:
                        continue
                    session.execute(insert(ShortCodeRegistry), row_chunk)
                    inserted_total += len(row_chunk)
                session.commit()
        return inserted_total

    def _claim_sequence_codes(self, amount: int) -> list[str]:
        if amount < 1:
            return []
        with self.db.sessions[0]() as session:
            state = self._get_or_create_generation_state(session)
            start = state.next_sequence_value
            state.next_sequence_value += amount
            state.updated_at = now_utc()
            session.commit()

        return [
            encode_number(value, min_length=self.settings.short_code_length)[-self.settings.short_code_length :]
            for value in range(start, start + amount)
        ]

    def _ensure_generation_state(self) -> None:
        with self.db.sessions[0]() as session:
            self._get_or_create_generation_state(session)
            session.commit()

    def _get_or_create_generation_state(self, session: Session) -> PoolGenerationState:
        state = session.get(PoolGenerationState, POOL_GENERATOR_NAME)
        if state is None:
            state = PoolGenerationState(generator_name=POOL_GENERATOR_NAME, next_sequence_value=0, updated_at=now_utc())
            session.add(state)
            session.flush()
        return state

    def _count_available(self) -> int:
        available = 0
        for factory in self.db.sessions:
            with factory() as session:
                available += (
                    session.scalar(
                        select(func.count())
                        .select_from(ShortCodeRegistry)
                        .where(ShortCodeRegistry.is_reserved.is_(False), ShortCodeRegistry.is_used.is_(False))
                    )
                    or 0
                )
        return available

    def _mark_code_consumed(self) -> None:
        with self._consumed_guard:
            self._consumed_since_last_check += 1

    def _needs_capacity_check(self) -> bool:
        with self._consumed_guard:
            if self._consumed_since_last_check < self.settings.pool_refill_check_interval:
                return False
            self._consumed_since_last_check = 0
            return True

    def _chunked(self, values, chunk_size: int):
        for start in range(0, len(values), chunk_size):
            yield values[start : start + chunk_size]
