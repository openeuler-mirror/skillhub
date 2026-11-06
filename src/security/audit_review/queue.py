"""Client-side rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections import deque

from src.security.audit_review.log import get_logger

logger = get_logger("queue")


class _RateLimiter:
    """简单的 60 秒滑动窗口限流（RPM + TPM），0 表示不限。

    同时累计等待时长与触发次数，供轮次汇总输出：限流等待是外部看起来
    "任务卡住但日志不动" 的常见原因。
    """

    def __init__(self, rpm: int, tpm: int, slow_wait_seconds: float = 30.0):
        self._rpm = max(0, rpm)
        self._tpm = max(0, tpm)
        self._lock = asyncio.Lock()
        self._events: deque[tuple[float, int]] = deque()
        self._slow_wait_seconds = max(1.0, slow_wait_seconds)
        self.total_wait_seconds = 0.0
        self.wait_events = 0
        self.slow_wait_events = 0

    async def acquire(self, tokens: int) -> None:
        if self._rpm <= 0 and self._tpm <= 0:
            return
        tokens = max(1, tokens)
        if self._tpm > 0:
            tokens = min(tokens, self._tpm)
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._events and now - self._events[0][0] >= 60.0:
                    self._events.popleft()
                wait = 0.0
                if self._rpm > 0 and len(self._events) >= self._rpm:
                    wait = max(wait, 60.0 - (now - self._events[0][0]))
                if self._tpm > 0:
                    used = sum(event_tokens for _, event_tokens in self._events)
                    if used + tokens > self._tpm and self._events:
                        wait = max(wait, 60.0 - (now - self._events[0][0]))
                if wait <= 0:
                    self._events.append((now, tokens))
                    break
                sleep_for = min(wait + 0.05, 5.0)
                await asyncio.sleep(sleep_for)
                waited += sleep_for

        if waited < 0.05:
            return
        self.total_wait_seconds += waited
        self.wait_events += 1
        if waited >= self._slow_wait_seconds:
            self.slow_wait_events += 1
            logger.warning(
                "rate limit waited %.1fs (rpm=%d, tpm=%d)", waited, self._rpm, self._tpm
            )
        else:
            logger.debug(
                "rate limit waited %.2fs (rpm=%d, tpm=%d)", waited, self._rpm, self._tpm
            )
