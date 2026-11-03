"""Client-side rate limiting and daily review window helpers."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta


class _RateLimiter:
    """简单的 60 秒滑动窗口限流（RPM + TPM），0 表示不限。"""

    def __init__(self, rpm: int, tpm: int):
        self._rpm = max(0, rpm)
        self._tpm = max(0, tpm)
        self._lock = asyncio.Lock()
        self._events: deque[tuple[float, int]] = deque()

    async def acquire(self, tokens: int) -> None:
        if self._rpm <= 0 and self._tpm <= 0:
            return
        tokens = max(1, tokens)
        if self._tpm > 0:
            tokens = min(tokens, self._tpm)
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
                    return
                await asyncio.sleep(min(wait + 0.05, 5.0))


# ═══════════════════════════════════════════════════════════════════════════
# 时间窗口
# ═══════════════════════════════════════════════════════════════════════════


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time (expected HH:MM): {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Invalid time (expected HH:MM): {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time (expected HH:MM): {value!r}")
    return hour, minute


def resolve_window(
    now: datetime, start: tuple[int, int], end: tuple[int, int]
) -> tuple[datetime, datetime] | None:
    """当前时刻在窗口内时返回 (窗口开始, 窗口结束)，否则返回 None。"""
    start_at = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    end_at = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    if start_at <= now < end_at:
        return start_at, end_at
    return None


def next_window_start(now: datetime, start: tuple[int, int]) -> datetime:
    """下一个窗口开始时刻（今天已过则顺延到明天）。"""
    candidate = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
