"""Date range that DaPrices.get_prices fetches when no end date is given."""

import datetime
import logging
from types import SimpleNamespace

import pytest

from dao.lib import da_prices


class _Db:
    def get_time_border_record(self, code):
        return None


def _fixed_now(value: datetime.datetime):
    class _Now(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return value

    return _Now


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        # a task scheduled at 12:00 that the scheduler starts 10 s early
        (datetime.datetime(2026, 11, 3, 11, 59, 50), "2026-11-03", "2026-11-05"),
        (datetime.datetime(2026, 11, 3, 11, 58, 0), "2026-11-03", "2026-11-04"),
        (datetime.datetime(2026, 11, 3, 12, 0, 5), "2026-11-03", "2026-11-05"),
        # the 00:00 run starts on the previous day
        (datetime.datetime(2026, 11, 3, 23, 59, 50), "2026-11-04", "2026-11-05"),
    ],
)
def test_end_follows_the_scheduled_hour(monkeypatch, caplog, now, start, end):
    monkeypatch.setattr(da_prices.datetime, "datetime", _fixed_now(now))
    prices = da_prices.DaPrices(SimpleNamespace(interval="1hour"), _Db())
    with caplog.at_level(logging.DEBUG):
        prices.get_prices("none")
    line = next(r.getMessage() for r in caplog.records if "tarieven ophalen" in r.getMessage())
    assert f"voor {start} 00:00:00" in line
    assert f"tot {end} 00:00:00" in line
