"""复核时间窗口与限流工具测试。"""

from datetime import datetime

import pytest

from src.security.audit_review.queue import (
    next_window_start,
    parse_hhmm,
    resolve_window,
)


class TestWindow:
    def test_parse_hhmm(self):
        assert parse_hhmm("02:00") == (2, 0)
        assert parse_hhmm("08:30") == (8, 30)

    @pytest.mark.parametrize("value", ["", "2", "24:00", "02:60", "aa:bb"])
    def test_parse_hhmm_invalid(self, value):
        with pytest.raises(ValueError):
            parse_hhmm(value)

    def test_resolve_window_inside(self):
        window = resolve_window(datetime(2026, 10, 29, 3, 0), (2, 0), (8, 0))
        assert window == (datetime(2026, 10, 29, 2, 0), datetime(2026, 10, 29, 8, 0))

    def test_resolve_window_outside(self):
        assert resolve_window(datetime(2026, 10, 29, 9, 0), (2, 0), (8, 0)) is None
        assert resolve_window(datetime(2026, 10, 29, 1, 59), (2, 0), (8, 0)) is None

    def test_next_window_start(self):
        assert next_window_start(datetime(2026, 10, 29, 9, 0), (2, 0)) == datetime(
            2026, 10, 30, 2, 0
        )
        assert next_window_start(datetime(2026, 10, 29, 1, 0), (2, 0)) == datetime(
            2026, 10, 29, 2, 0
        )
