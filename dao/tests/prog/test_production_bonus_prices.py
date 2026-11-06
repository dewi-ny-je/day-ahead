"""
Report.get_price_data with a production bonus, on a fake database
(no Home Assistant or real database needed).
"""

import datetime
import os
import time
import types

import pandas as pd
import pytest

from dao.prog import da_report
from dao.prog.config.models.pricing import ProductionBonusConfig
from dao.prog.da_report import Report
from dao.prog.production_bonus import ProductionBonus

QUARTER = 900
OL_T = 0.02


@pytest.fixture(autouse=True)
def amsterdam_tz():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Amsterdam"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def ts(*args) -> float:
    return datetime.datetime(*args).timestamp()


class FakeDb:
    """Quarter prices in "values" and hourly prices in "prognoses", like DaDb.get_column_data."""

    def __init__(self, values: dict[float, float], prognoses: dict[float, float] = None):
        self.tables = {"values": values, "prognoses": prognoses or {}}

    def get_column_data(self, tablename, column_name, start=None, end=None, agg_func=None):
        assert agg_func is None
        # like the real query: naive local time (also for pd.Timestamp, whose
        # timestamp() would treat it as utc)
        def local_ts(moment) -> float:
            return datetime.datetime.strptime(
                moment.strftime("%Y-%m-%d %H:%M"), "%Y-%m-%d %H:%M"
            ).timestamp()

        start_ts = local_ts(start)
        end_ts = local_ts(end) if end is not None else float("inf")
        rows = [
            (utc, value)
            for utc, value in sorted(self.tables[tablename].items())
            if start_ts <= utc < end_ts
        ]
        return pd.DataFrame(
            {
                "time": [
                    datetime.datetime.fromtimestamp(utc).strftime("%Y-%m-%d %H:%M")
                    for utc, _ in rows
                ],
                "utc": [utc for utc, _ in rows],
                "value": [value for _, value in rows],
            }
        )


def make_report(db: FakeDb, **bonus_kwargs) -> Report:
    report = Report.__new__(Report)
    report.db_da = db
    report.prices_options = None
    report.ol_l_def = {"2025-01-01": 0.0}
    report.ol_t_def = {"2025-01-01": OL_T}
    report.taxes_l_def = {"2025-01-01": 0.0}
    report.taxes_t_def = {"2025-01-01": 0.0}
    report.btw_l_def = {"2025-01-01": 0}
    report.btw_t_def = {"2025-01-01": 0}
    report.multiplier_l_def = {"2025-01-01": 1}
    report.multiplier_t_def = {"2025-01-01": 1}
    config = ProductionBonusConfig(
        **{"percentage": {"2025-01-01": 10}, "window": "fixed", **bonus_kwargs}
    )
    report.production_bonus = ProductionBonus(config, latitude=52.1, longitude=5.2)
    return report


def quarters(start_ts: float, end_ts: float, price) -> dict[float, float]:
    result = {}
    utc = start_ts
    while utc < end_ts:
        result[utc] = price(utc)
        utc += QUARTER
    return result


def with_bonus(price: float) -> float:
    return (price + OL_T) * 1.1


# 25 Oct 2026: 02:00-03:00 local time occurs twice (25 hours)
DST_START = datetime.datetime(2026, 10, 25)
DST_END = datetime.datetime(2026, 10, 26)


def dst_prices() -> dict[float, float]:
    # each utc hour its own price, so a shifted hour shows up
    return quarters(
        DST_START.timestamp(), DST_END.timestamp(), lambda u: 0.10 + (u // 3600) % 25 / 100
    )


class TestFallBackDay:
    def test_hourly_has_25_hours(self):
        values = dst_prices()
        report = make_report(FakeDb(values), start="01:00", end="05:00")
        df = report.get_price_data(DST_START, DST_END, interval="1hour")
        assert len(df) == 25
        utc_hours = sorted({u // 3600 for u in values})
        for row, hour in zip(df.itertuples(), utc_hours, strict=False):
            assert row.da_ex == pytest.approx(values[hour * 3600])
        # both 02:00 hours lie inside the window
        times = [t.strftime("%H:%M") for t in df["time"]]
        assert times[2] == times[3] == "02:00"
        for i in (2, 3):
            assert df["da_prod"].iloc[i] == pytest.approx(with_bonus(df["da_ex"].iloc[i]))

    def test_repeated_quarters_get_bonus(self):
        values = dst_prices()
        # the window ends 02:45 + 15 min; all eight 02:xx quarters are inside it
        report = make_report(FakeDb(values), start="01:00", end="03:00")
        df = report.get_price_data(DST_START, DST_END, interval="15min")
        assert len(df) == 100
        repeated = df[df["time"].dt.hour == 2]
        assert len(repeated) == 8
        for row in repeated.itertuples():
            assert row.da_prod == pytest.approx(with_bonus(row.da_ex))
        after = df[df["time"].dt.hour == 3]
        for row in after.itertuples():
            assert row.da_prod == pytest.approx(row.da_ex + OL_T)


class TestHourlyAverage:
    def test_hour_is_mean_of_quarters(self):
        start = datetime.datetime(2026, 6, 1, 10)
        # the window starts at 10:30, so only the last two quarters get the bonus
        prices = {0: 0.10, 1: 0.20, 2: 0.30, 3: 0.40}
        values = quarters(
            start.timestamp(),
            start.timestamp() + 3600,
            lambda u: prices[int(u - start.timestamp()) // QUARTER],
        )
        report = make_report(FakeDb(values), start="10:30", end="12:00")
        df = report.get_price_data(start, start + datetime.timedelta(hours=1))
        assert len(df) == 1
        assert df["time"].iloc[0] == start
        assert df["da_ex"].iloc[0] == pytest.approx(0.25)
        expected = (0.10 + OL_T + 0.20 + OL_T + with_bonus(0.30) + with_bonus(0.40)) / 4
        assert df["da_prod"].iloc[0] == pytest.approx(expected)

    def test_without_bonus_unchanged(self):
        start = datetime.datetime(2026, 6, 1, 10)
        values = quarters(start.timestamp(), start.timestamp() + 3600, lambda u: 0.10)
        report = make_report(FakeDb(values))
        report.production_bonus = None
        db = report.db_da
        # without bonus the database averages the hour
        db.get_column_data = lambda *a, agg_func=None, **k: FakeDb.get_column_data(
            db, *a, **k
        ).iloc[:1]
        df = report.get_price_data(start, start + datetime.timedelta(hours=1))
        assert list(df.columns) == ["time", "da_ex", "da_cons", "da_prod", "datasoort", "slot"]
        # "slot" is the start of the hour in UTC
        assert df["slot"].iloc[0] == int(start.timestamp())
        assert df["da_prod"].iloc[0] == pytest.approx(0.10 + OL_T)


class TestPrediction:
    def test_interpolated_rows_get_bonus(self):
        start = datetime.datetime(2026, 6, 1, 10)
        values = quarters(start.timestamp(), start.timestamp() + 3600, lambda u: 0.10)
        prognoses = {
            start.timestamp() + h * 3600: 0.10 + h / 100 for h in range(1, 4)
        }
        report = make_report(FakeDb(values, prognoses), start="10:00", end="13:00")
        df = report.get_price_data(
            start, start + datetime.timedelta(hours=1), interval="15min", extension=3
        )
        assert len(df) == 16
        assert list(df["time"]) == [
            start + datetime.timedelta(minutes=15 * i) for i in range(16)
        ]
        for row in df.itertuples():
            assert row.datasoort == "expected"
            if row.time.hour < 13:
                assert row.da_prod == pytest.approx(with_bonus(row.da_ex))
            else:
                assert row.da_prod == pytest.approx(row.da_ex + OL_T)


class ColumnDb(FakeDb):
    """Like FakeDb, with a table per (table, column) and a log of the queried ranges."""

    def __init__(self, columns: dict[tuple[str, str], dict[float, float]]):
        super().__init__({})
        self.columns = columns
        self.queries = []

    def get_column_data(self, tablename, column_name, start=None, end=None, agg_func=None):
        self.queries.append((tablename, column_name, start, end))
        self.tables = {tablename: self.columns[(tablename, column_name)]}
        return super().get_column_data(tablename, column_name, start, end, agg_func)


def fake_now(monkeypatch, moment: datetime.datetime):
    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(moment.timestamp(), tz)

    monkeypatch.setattr(
        da_report,
        "datetime",
        types.SimpleNamespace(datetime=FixedDatetime, timedelta=datetime.timedelta),
    )


YEAR_START = datetime.datetime(2026, 1, 1)


def counter_db(until: datetime.datetime) -> ColumnDb:
    # 1 kWh per hour, a constant positive price
    prod = {
        YEAR_START.timestamp() + h * 3600: 1.0
        for h in range(int((until - YEAR_START).total_seconds() // 3600))
    }
    prices = quarters(YEAR_START.timestamp(), until.timestamp(), lambda u: 0.10)
    return ColumnDb({("values", "prod"): prod, ("values", "da"): prices})


def counter_report(db, tmp_path, cap=None) -> Report:
    # window 08:00-18:00: 10 kWh bonus per day
    report = make_report(db, start="08:00", end="18:00", **{"annual cap": cap})
    report.file_name = str(tmp_path / "options.json")
    report.grid_production_sensors = []
    return report


def prod_starts(db: ColumnDb) -> list:
    return [q[2] for q in db.queries if q[1] == "prod"]


class TestAnnualCapCounter:
    def test_second_run_fetches_only_the_tail(self, tmp_path, monkeypatch):
        now = datetime.datetime(2026, 6, 10, 12)
        db = counter_db(datetime.datetime(2026, 7, 1))
        report = counter_report(db, tmp_path, cap=7500)
        fake_now(monkeypatch, now)
        first = report._production_bonus_usage(2026)
        assert prod_starts(db) == [YEAR_START]
        days = (now - YEAR_START).days
        assert first.used_before(now.timestamp()) == pytest.approx(days * 10 + 4)
        assert (tmp_path / "production_bonus_counter.json").exists()

        later = now + datetime.timedelta(days=1, hours=3)
        fake_now(monkeypatch, later)
        db.queries.clear()
        second = report._production_bonus_usage(2026)
        # the counter stops two days before the previous run
        assert prod_starts(db) == [now - datetime.timedelta(days=2)]
        expected = (later - YEAR_START).days * 10 + 7
        assert second.used_before(later.timestamp()) == pytest.approx(expected)

        # same result as a full recomputation
        (tmp_path / "production_bonus_counter.json").unlink()
        db.queries.clear()
        full = report._production_bonus_usage(2026)
        assert prod_starts(db) == [YEAR_START]
        assert full.used_before(later.timestamp()) == pytest.approx(expected)

    def test_reached_cap_kept_in_counter(self, tmp_path, monkeypatch):
        # 105 kWh: reached on the 11th day at 13:00 (100 kWh before, 5 more)
        reached = datetime.datetime(2026, 1, 11, 13)
        db = counter_db(datetime.datetime(2026, 3, 1))
        report = counter_report(db, tmp_path, cap=105)
        fake_now(monkeypatch, datetime.datetime(2026, 2, 1))
        first = report._production_bonus_usage(2026)
        assert first.reached_at(105) == reached.timestamp()

        fake_now(monkeypatch, datetime.datetime(2026, 2, 20))
        db.queries.clear()
        second = report._production_bonus_usage(2026)
        assert prod_starts(db) == [datetime.datetime(2026, 1, 30)]
        assert not second.cap_reached(reached.timestamp() - 1, 105)
        assert second.cap_reached(reached.timestamp(), 105)
        assert second.cap_reached(datetime.datetime(2026, 2, 19).timestamp(), 105)

    def test_other_settings_recompute(self, tmp_path, monkeypatch):
        db = counter_db(datetime.datetime(2026, 3, 1))
        fake_now(monkeypatch, datetime.datetime(2026, 2, 1))
        counter_report(db, tmp_path, cap=7500)._production_bonus_usage(2026)
        db.queries.clear()
        usage = counter_report(db, tmp_path, cap=105)._production_bonus_usage(2026)
        assert prod_starts(db) == [YEAR_START]
        assert usage.reached_at(105) == datetime.datetime(2026, 1, 11, 13).timestamp()

    def test_finished_year_needs_no_query(self, tmp_path, monkeypatch):
        db = counter_db(datetime.datetime(2027, 1, 1))
        report = counter_report(db, tmp_path, cap=7500)
        fake_now(monkeypatch, datetime.datetime(2027, 1, 10))
        first = report._production_bonus_usage(2026)
        assert first.used_before(datetime.datetime(2027, 1, 1).timestamp()) == pytest.approx(3650)
        db.queries.clear()
        second = report._production_bonus_usage(2026)
        assert prod_starts(db) == []
        assert second.used_before(datetime.datetime(2027, 1, 1).timestamp()) == pytest.approx(3650)

    def test_zero_cap_skips_counting(self, tmp_path, monkeypatch):
        db = counter_db(datetime.datetime(2026, 3, 1))
        report = counter_report(db, tmp_path, cap=0)
        report._bonus_usage_cache = {}
        fake_now(monkeypatch, datetime.datetime(2026, 2, 1))
        start = ts(2026, 1, 20, 12)
        assert report._production_bonus_price(start, QUARTER, 0.10, OL_T) == pytest.approx(
            0.10 * (0.10 + OL_T)
        )
        assert prod_starts(db) == []
        assert not (tmp_path / "production_bonus_counter.json").exists()
