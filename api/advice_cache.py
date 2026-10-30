"""AI 建議的記憶體快取、呼叫間隔與硬逾時。各 Cloud Run 實例各自計算。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime
from threading import Lock


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__("rate limited")


class AdviceTimeout(Exception):
    pass


@dataclass
class CachedAdvice:
    text: str
    generated_at: datetime
    cached: bool


class AdviceCache:
    def __init__(self, ttl_seconds: float, min_interval_seconds: float, clock=None):
        self.ttl = float(ttl_seconds)
        self.min_interval = float(min_interval_seconds)
        self.clock = clock or time.monotonic
        self._lock = Lock()
        self._text: str | None = None
        self._generated_mono: float | None = None
        self._generated_at: datetime | None = None
        self._last_attempt_mono: float | None = None

    def lookup(self) -> CachedAdvice | None:
        """新鮮快取直接回。冷卻中沒有快取則丟 RateLimited。要重新產生時回 None。"""
        with self._lock:
            return self._lookup_locked()

    def generate(self, producer, now_wall: datetime) -> CachedAdvice:
        """同一個實例同時只會打一次模型。producer 在鎖內執行。"""
        with self._lock:
            ready = self._lookup_locked()
            if ready is not None:
                return ready
            self._last_attempt_mono = self.clock()
            text = producer()
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("empty advice")
            self._text = text
            self._generated_mono = self.clock()
            self._generated_at = now_wall
            return CachedAdvice(text, now_wall, False)

    def _lookup_locked(self) -> CachedAdvice | None:
        now = self.clock()
        if self._is_fresh(now):
            return CachedAdvice(self._text, self._generated_at, True)
        if self._is_cooling(now):
            if self._text is not None:
                return CachedAdvice(self._text, self._generated_at, True)
            retry = int(self.min_interval - (now - self._last_attempt_mono) + 0.999)
            raise RateLimited(retry)
        return None

    def _is_fresh(self, now: float) -> bool:
        return (
            self._text is not None
            and self._generated_mono is not None
            and (now - self._generated_mono) < self.ttl
        )

    def _is_cooling(self, now: float) -> bool:
        return self._last_attempt_mono is not None and (now - self._last_attempt_mono) < self.min_interval


def call_with_timeout(fn, timeout_seconds: float):
    """超過秒數就丟 AdviceTimeout。背景那一輪仍會自己結束，但呼叫端不用等。"""
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        return future.result(timeout=float(timeout_seconds))
    except FuturesTimeoutError as exc:
        raise AdviceTimeout() from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
