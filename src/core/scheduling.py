"""定时任务共享的时间计算原语。

discover 扫描（单点触发）与 audit LLM 复核（起止窗口）共用同一套时区与
时间解析语义：时间字符串统一解析为 24 小时制 ``(hour, minute)``，触发/窗口
时刻统一按可配置时区（默认北京时间）解释，与容器 TZ 解耦。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# 未显式配置时按北京时间解释定时时刻，避免依赖容器 TZ（线上容器通常为 UTC）
DEFAULT_TIMEZONE = "Asia/Shanghai"


def resolve_timezone(name: str | None) -> ZoneInfo:
    """解析定时时区；留空时回退默认时区。"""
    return ZoneInfo((name or DEFAULT_TIMEZONE).strip())


def now_in(tz: ZoneInfo) -> datetime:
    """当前时刻（带配置时区），触发点/窗口/截止判断统一用它。"""
    return datetime.now(tz)


def parse_hhmm(value: str) -> tuple[int, int]:
    """把 HH:MM 解析为 (hour, minute)。"""
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


def next_daily(now: datetime, start: tuple[int, int]) -> datetime:
    """下一个每日触发时刻（今天已过 start 则顺延到明天）。"""
    candidate = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def resolve_window(
    now: datetime, start: tuple[int, int], end: tuple[int, int]
) -> tuple[datetime, datetime] | None:
    """当前时刻在 [start, end) 窗口内时返回 (窗口开始, 窗口结束)，否则返回 None。"""
    start_at = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    end_at = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    if start_at <= now < end_at:
        return start_at, end_at
    return None