import json
import time
from collections import deque
from threading import Condition
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from url_shortener.core.config import Settings, get_settings


class Cache:
    def __init__(self, settings: Settings):
        self._memory: dict[str, tuple[float, str]] = {}
        self._queues: dict[str, deque[str]] = {}
        self._queue_condition = Condition()
        self._redis: Redis | None = None
        if settings.redis_url:
            try:
                self._redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=0.2)
                self._redis.ping()
            except RedisError:
                self._redis = None

    def get_json(self, key: str) -> dict[str, Any] | None:
        if self._redis:
            value = self._redis.get(key)
            return json.loads(value) if value else None
        item = self._memory.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < time.time():
            self._memory.pop(key, None)
            return None
        return json.loads(value)

    def set_json_forever(self, key: str, value: dict[str, Any]) -> None:
        payload = json.dumps(value, default=str)
        if self._redis:
            self._redis.set(key, payload)
            return
        self._memory[key] = (time.time() + 60 * 60 * 24 * 365, payload)

    def set_json(self, key: str, value: dict[str, Any], ttl: int) -> None:
        payload = json.dumps(value, default=str)
        if self._redis:
            self._redis.setex(key, ttl, payload)
            return
        self._memory[key] = (time.time() + ttl, payload)

    def delete(self, key: str) -> None:
        if self._redis:
            self._redis.delete(key)
            return
        self._memory.pop(key, None)

    def incr_with_ttl(self, key: str, ttl: int) -> int:
        if self._redis:
            pipe = self._redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl)
            value, _ = pipe.execute()
            return int(value)
        now = time.time()
        expires_at, raw = self._memory.get(key, (now + ttl, "0"))
        if expires_at < now:
            expires_at, raw = now + ttl, "0"
        value = int(raw) + 1
        self._memory[key] = (expires_at, str(value))
        return value

    def push_json_queue(self, queue_name: str, value: dict[str, Any]) -> None:
        payload = json.dumps(value, default=str)
        if self._redis:
            self._redis.lpush(queue_name, payload)
            return
        with self._queue_condition:
            queue = self._queues.setdefault(queue_name, deque())
            queue.appendleft(payload)
            self._queue_condition.notify_all()

    def pop_json_queue(self, queue_name: str, timeout: int) -> dict[str, Any] | None:
        if self._redis:
            value = self._redis.brpop(queue_name, timeout=timeout)
            if not value:
                return None
            _, payload = value
            return json.loads(payload)
        deadline = time.time() + timeout
        with self._queue_condition:
            while True:
                queue = self._queues.setdefault(queue_name, deque())
                if queue:
                    return json.loads(queue.pop())
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._queue_condition.wait(timeout=remaining)


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(get_settings())
    return _cache
