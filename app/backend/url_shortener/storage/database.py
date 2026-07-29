from datetime import UTC, datetime
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from url_shortener.core.config import Settings, get_settings
from url_shortener.storage.models import Base


class ShardedDatabase:
    def __init__(self, settings: Settings):
        urls = settings.database_urls
        if len(urls) == 1 and "," in urls[0]:
            urls = [part.strip() for part in urls[0].split(",") if part.strip()]
        replica_urls = settings.database_replica_urls or urls
        if len(replica_urls) == 1 and isinstance(replica_urls[0], str) and "," in replica_urls[0]:
            replica_urls = [part.strip() for part in replica_urls[0].split(",") if part.strip()]

        self.write_engines = [
            create_engine(
                url,
                pool_pre_ping=True,
                future=True,
                connect_args={"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {},
            )
            for url in urls
        ]
        self.read_engines = [
            create_engine(
                url,
                pool_pre_ping=True,
                future=True,
                connect_args={"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {},
            )
            for url in replica_urls
        ]
        self.sessions = [sessionmaker(bind=engine, expire_on_commit=False, future=True) for engine in self.write_engines]
        self.read_sessions = [sessionmaker(bind=engine, expire_on_commit=False, future=True) for engine in self.read_engines]

    def create_all(self) -> None:
        for engine in self.write_engines:
            Base.metadata.create_all(engine)
            self._ensure_operational_postgres_objects(engine)

    def shard_index(self, key: str) -> int:
        digest = sha256(key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % len(self.sessions)

    @contextmanager
    def session_for_key(self, key: str) -> Iterator[Session]:
        session = self.sessions[self.shard_index(key)]()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def read_session_for_key(self, key: str) -> Iterator[Session]:
        session = self.read_sessions[self.shard_index(key)]()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def control_session(self) -> Iterator[Session]:
        session = self.sessions[0]()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def control_read_session(self) -> Iterator[Session]:
        session = self.read_sessions[0]()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def each_session(self) -> Iterator[Session]:
        sessions = [factory() for factory in self.sessions]
        try:
            for session in sessions:
                yield session
                session.commit()
        except Exception:
            for session in sessions:
                session.rollback()
            raise
        finally:
            for session in sessions:
                session.close()

    @contextmanager
    def each_read_session(self) -> Iterator[Session]:
        sessions = [factory() for factory in self.read_sessions]
        try:
            for session in sessions:
                yield session
        finally:
            for session in sessions:
                session.close()

    def _ensure_operational_postgres_objects(self, engine) -> None:
        if engine.dialect.name != "postgresql":
            return
        current_month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_starts = [current_month]
        for _ in range(2):
            year = month_starts[-1].year + (1 if month_starts[-1].month == 12 else 0)
            month = 1 if month_starts[-1].month == 12 else month_starts[-1].month + 1
            month_starts.append(month_starts[-1].replace(year=year, month=month))

        with engine.begin() as connection:
            connection.execute(
                text(
                    'CREATE TABLE IF NOT EXISTS "DAT_LINK_REDIRECT_EVENTS_DEFAULT" '
                    'PARTITION OF "DAT_LINK_REDIRECT_EVENTS" DEFAULT'
                )
            )
            for start in month_starts:
                if start.month == 12:
                    end = start.replace(year=start.year + 1, month=1)
                else:
                    end = start.replace(month=start.month + 1)
                partition_name = f'DAT_LINK_REDIRECT_EVENTS_{start.year}_{start.month:02d}'
                start_literal = start.isoformat()
                end_literal = end.isoformat()
                connection.execute(
                    text(
                        f'CREATE TABLE IF NOT EXISTS "{partition_name}" '
                        'PARTITION OF "DAT_LINK_REDIRECT_EVENTS" '
                        f"FOR VALUES FROM ('{start_literal}') TO ('{end_literal}')"
                    )
                )


_database: ShardedDatabase | None = None


def get_database() -> ShardedDatabase:
    global _database
    if _database is None:
        _database = ShardedDatabase(get_settings())
    return _database


def reset_database_for_tests(settings: Settings) -> ShardedDatabase:
    global _database
    _database = ShardedDatabase(settings)
    _database.create_all()
    return _database
