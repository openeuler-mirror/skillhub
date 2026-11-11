"""日志时间时区转换工具，api 与 worker 共用。

日志时间按 logging.timezone（缺省 Asia/Shanghai）展示，与容器 TZ / 系统时钟
无关，保证所有进程口径一致。
"""

from __future__ import annotations

import time
from datetime import datetime

from src.core.config import get_settings
from src.core.scheduling import resolve_timezone

_LOG_TZ = resolve_timezone(get_settings().logging.timezone)


def log_localtime(seconds: float | None = None) -> time.struct_time:
    """epoch 秒 → 配置时区的本地时间，供 Formatter.converter 使用。"""
    if seconds is None:
        seconds = time.time()
    return datetime.fromtimestamp(seconds, _LOG_TZ).timetuple()