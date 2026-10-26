"""Keep each real hour apart in reports on the days daylight saving time changes."""

import datetime as dt
import os
import threading
import time
import types

import pandas as pd
import pytest
from sqlalchemy import (
    BigInteger,
    Column,
    Double,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
)
from sqlalchemy.engine import make_url

from dao.lib.db_manager import DBmanagerObj
from dao.prog import da_report
from dao.prog.da_report import Report

# Europe/Amsterdam: 02:00-03:00 repeats on 25 Oct 2026, 02:00 is skipped on 29 Mar 2026.
WINTER_DAY = dt.datetime(2026, 10, 25)
SUMMER_DAY = dt.datetime(2026, 3, 29)


@pytest.fixture(autouse=True)
def amsterdam_time():
    # SQLite "localtime" and naive datetime.timestamp() use the process time zone.
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Amsterdam"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture
def frozen_now(monkeypatch):
    """
    Sets the report's datetime.now(). freezegun can't be used here: its
    fromtimestamp() ignores the process time zone.
    """

    class FrozenDatetime(dt.datetime):
        moment = None

        @classmethod
        def now(cls, tz=None):
            return cls.moment

    module = types.SimpleNamespace(**{name: getattr(dt, name) for name in dir(dt) if not name.startswith("__")})
    module.datetime = FrozenDatetime
    monkeypatch.setattr(da_report, "datetime", module)

    def freeze(moment: dt.datetime):
        FrozenDatetime.moment = moment

    return freeze


DATABASES = {
    # disposable server databases, as in test_ha_data_query.py
    "mariadb": ("DAO_TEST_MARIADB_URL", "mysql",
                {"init_command": "SET time_zone = 'Europe/Amsterdam'"}),
    "postgresql": ("DAO_TEST_POSTGRESQL_URL", "postgresql",
                   {"options": "-c timezone=Europe/Amsterdam"}),
}


@pytest.fixture(params=["sqlite", "mariadb", "postgresql"])
def db(request):
    if request.param != "sqlite":
        env, backend, _ = DATABASES[request.param]
        url = os.getenv(env)
        if not url or os.getenv("DAO_TEST_ALLOW_SCHEMA_RESET") != "1":
            pytest.skip(f"disposable {request.param} test database not configured")
        url = make_url(url)
        if url.database != "dao_query_test" or url.get_backend_name() != backend:
            raise ValueError("database fixtures require the dedicated dao_query_test schema")
    database = types.SimpleNamespace(dialect=request.param, engines=[])
    yield database
    for engine, metadata in database.engines:
        if request.param != "sqlite":
            metadata.drop_all(engine)
        engine.dispose()


def make_manager(metadata: MetaData, db=None) -> DBmanagerObj:
    dialect = "sqlite" if db is None else db.dialect
    manager = DBmanagerObj.__new__(DBmanagerObj)
    if dialect == "sqlite":
        manager.db_dialect = "sqlite"
        manager.engine = create_engine("sqlite:///:memory:")
    else:
        env, backend, connect_args = DATABASES[dialect]
        manager.db_dialect = backend
        manager.engine = create_engine(make_url(os.environ[env]), connect_args=connect_args)
        metadata.drop_all(manager.engine)
    manager.metadata = MetaData()
    manager._metadata_lock = threading.RLock()
    metadata.create_all(manager.engine)
    if db is not None:
        db.engines.append((manager.engine, metadata))
    return manager


def da_database(rows: dict[str, list[tuple[int, float]]], forecasts=None, db=None) -> DBmanagerObj:
    metadata = MetaData()
    variables = Table("variabel", metadata, Column("id", Integer, primary_key=True), Column("code", String(20)))
    tables = {
        name: Table(name, metadata, Column("time", BigInteger), Column("variabel", Integer), Column("value", Float))
        for name in ("values", "prognoses")
    }
    manager = make_manager(metadata, db)
    codes = sorted(set(rows) | set(forecasts or {}))
    with manager.engine.begin() as connection:
        connection.execute(variables.insert(), [{"id": i, "code": code} for i, code in enumerate(codes, 1)])
        for name, data in (("values", rows), ("prognoses", forecasts or {})):
            records = [
                {"time": ts, "variabel": codes.index(code) + 1, "value": value}
                for code, points in data.items()
                for ts, value in points
            ]
            if records:
                connection.execute(tables[name].insert(), records)
    return manager


def ha_database(sensor: str, states: list[tuple[int, float]], db=None) -> DBmanagerObj:
    metadata = MetaData()
    meta = Table(
        "statistics_meta", metadata,
        Column("id", Integer, primary_key=True), Column("statistic_id", String(255)),
        Column("unit_of_measurement", String(20)),
    )
    statistics = Table(
        "statistics", metadata,
        Column("id", Integer, primary_key=True), Column("metadata_id", Integer),
        Column("start_ts", Double), Column("state", Double), Column("mean", Double),
    )
    manager = make_manager(metadata, db)
    with manager.engine.begin() as connection:
        connection.execute(meta.insert(), [{"id": 1, "statistic_id": sensor, "unit_of_measurement": "kWh"}])
        connection.execute(statistics.insert(), [
            {"metadata_id": 1, "start_ts": float(ts), "state": state, "mean": None} for ts, state in states
        ])
    return manager


def hours_of(day: dt.datetime) -> list[int]:
    start = int(day.timestamp())
    end = int((day + dt.timedelta(days=1)).timestamp())
    return list(range(start, end, 3600))


def make_report(db_da=None, db_ha=None) -> Report:
    report = Report.__new__(Report)
    report.db_da = db_da
    report.db_ha = db_ha
    report.interval = "1hour"
    report.time_zone = "Europe/Amsterdam"
    return report


@pytest.mark.parametrize("day, hours", [(WINTER_DAY, 25), (SUMMER_DAY, 23)])
def test_time_grid_counts_real_hours(day, hours):
    grid = Report.generate_df(day, day + dt.timedelta(days=1), "uur")
    assert len(grid) == hours
    assert grid["slot"].tolist() == hours_of(day)
    labels = grid["uur"].str.strip().tolist()
    assert labels.count("02:00") == (2 if day == WINTER_DAY else 0)


def test_grid_report_keeps_both_hours_of_the_repeated_hour(db):
    slots = hours_of(WINTER_DAY)
    rows = {
        code: [(ts, factor * (i + 1)) for i, ts in enumerate(slots)]
        for code, factor in (("cons", 1.0), ("prod", 0.1), ("cost", 0.25), ("profit", 0.01))
    }
    report = make_report(db_da=da_database(rows, db=db))
    end = WINTER_DAY + dt.timedelta(days=1)
    result = report.get_grid_data("", _vanaf=WINTER_DAY, _tot=end, _interval="uur", _source="da")

    assert len(result) == 25
    assert result["slot"].tolist() == slots
    assert result["consumption"].tolist() == [float(i + 1) for i in range(25)]
    assert result["uur"].tolist().count("02:00") == 2
    assert result["consumption"].sum() == sum(range(1, 26))


def test_grid_report_combines_ha_forecasts_and_prices_per_real_hour(db, frozen_now):
    slots = hours_of(WINTER_DAY)
    # recorded in HA until 04:00 winter time (5 real hours), forecasts after that
    recorded, expected = slots[:5], slots[5:]
    forecasts = {"cons": [(ts, 10.0 + i) for i, ts in enumerate(expected)],
                 "prod": [(ts, 1.0) for ts in expected]}
    report = make_report(db_da=da_database({"cost": [], "profit": []}, forecasts, db=db))
    report.grid_consumption_sensors = ["sensor.cons"]
    report.grid_production_sensors = ["sensor.prod"]

    def sensor_data(sensor, vanaf, tot, col_name, agg="uur", sensor_type="quantity"):
        value = 1.0 if col_name == "consumption" else 0.0
        return pd.DataFrame({
            "tijd": [pd.Timestamp(dt.datetime.fromtimestamp(ts)) for ts in recorded],
            col_name: [value * (i + 1) for i in range(len(recorded))],
            "slot": recorded,
        })

    def price_data(vanaf, tot, interval="1hour", extension=0):
        utc = [ts for ts in slots if vanaf.timestamp() <= ts < tot.timestamp()]
        return pd.DataFrame({
            "time": [dt.datetime.fromtimestamp(ts) for ts in utc],
            "da_cons": [0.1 * (i + 1) for i, _ in enumerate(utc)],
            "da_prod": 0.05,
            "slot": utc,
        })

    report.get_sensor_data = sensor_data
    report.get_price_data = price_data
    end = WINTER_DAY + dt.timedelta(days=1)
    frozen_now(dt.datetime(2026, 10, 25, 4, 30))
    result = report.get_grid_data("", _vanaf=WINTER_DAY, _tot=end, _interval="uur", _source="all")

    assert len(result) == 25
    assert result["slot"].tolist() == slots
    # the two 02:00 hours are slots 2 and 3, each with its own energy and price
    assert result["consumption"].tolist()[:5] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert result["cost"].tolist()[2:4] == pytest.approx([3.0 * 0.3, 4.0 * 0.4])
    assert result["consumption"].tolist()[5:] == [10.0 + i for i in range(20)]
    table = report.calc_grid_columns(result, "uur", "tabel")
    assert table.iloc[:-1, 0].str.strip().tolist().count("02:00") == 2


def test_ha_sensor_data_counts_each_real_hour(db, frozen_now):
    sensor = "sensor.energy"
    # HA statistics: state is the meter reading at the end of the hour that starts at start_ts
    slots = hours_of(WINTER_DAY)
    states, total = [(slots[0] - 3600, 0.0)], 0.0
    for i, ts in enumerate(slots):
        if i == 7:  # no statistics for this hour, so hours 7 and 8 have no difference
            continue
        total += i + 1
        states.append((ts, total))
    report = make_report(db_ha=ha_database(sensor, states, db=db))
    end = WINTER_DAY + dt.timedelta(days=1)
    frozen_now(dt.datetime(2026, 10, 26, 12))
    result = report.get_sensor_data(sensor, WINTER_DAY, end, "value", "uur")

    assert result["slot"].tolist() == slots
    values = result["value"].tolist()
    assert values[:7] == [float(i + 1) for i in range(7)]
    # the hours without a difference are added with 0
    assert values[7:9] == [0.0, 0.0]
    assert values[9:] == [float(i + 1) for i in range(9, 25)]


def test_ha_sensor_data_adds_no_hour_that_does_not_exist(db, frozen_now):
    sensor = "sensor.energy"
    slots = hours_of(SUMMER_DAY)
    states = [(slots[0] - 3600, 0.0)] + [(ts, float(i + 1)) for i, ts in enumerate(slots)]
    report = make_report(db_ha=ha_database(sensor, states, db=db))
    end = SUMMER_DAY + dt.timedelta(days=1)
    frozen_now(dt.datetime(2026, 3, 30, 12))
    result = report.get_sensor_data(sensor, SUMMER_DAY, end, "value", "uur")

    assert len(result) == 23
    assert result["slot"].tolist() == slots
    assert "02:00" not in result["uur"].tolist()


def test_da_data_and_columns_keep_the_repeated_hour(db):
    slots = hours_of(WINTER_DAY)
    report = make_report(db_da=da_database({"gr": [(ts, float(i)) for i, ts in enumerate(slots)]}, db=db))
    end = WINTER_DAY + dt.timedelta(days=1)
    data = report.get_da_data("gr", WINTER_DAY, end, "uur", "uur")
    assert data["slot"].tolist() == slots
    grid = Report.generate_df(WINTER_DAY, end, "uur", column="gr")
    Report.add_col_df(data, grid, "gr")
    assert grid["gr"].tolist() == [float(i) for i in range(25)]


def test_prices_average_per_real_hour(db):
    slots = hours_of(WINTER_DAY)
    quarters = [(ts + q * 900, float(i)) for i, ts in enumerate(slots) for q in range(4)]
    report = make_report(db_da=da_database({"da": quarters}, db=db))
    report.prices_options = None
    for name in ("ol_l", "ol_t", "taxes_l", "taxes_t", "btw_l", "btw_t"):
        setattr(report, f"{name}_def", {"2026-01-01": 0})
    report.multiplier_l_def = report.multiplier_t_def = {"2026-01-01": 1}
    end = WINTER_DAY + dt.timedelta(days=1)
    prices = report.get_price_data(WINTER_DAY, end)

    assert len(prices) == 25
    assert prices["slot"].tolist() == slots
    assert prices["da_ex"].tolist() == [float(i) for i in range(25)]


def test_recalc_groups_quarters_per_real_hour():
    slots = hours_of(WINTER_DAY)[:5]
    quarters = [ts + q * 900 for ts in slots for q in range(4)]
    rows = pd.DataFrame({
        "tijd": [pd.Timestamp(dt.datetime.fromtimestamp(ts)) for ts in quarters],
        "consumption": 0.25,
        "production": 0.0,
        "da_cons": 1.0,
        "da_prod": 1.0,
        "datasoort": "expected",
        "slot": quarters,
    })
    result = make_report().recalc_df_ha(rows, "uur")
    assert result["slot"].tolist() == slots
    assert result["consumption"].tolist() == [1.0] * 5
    assert result["uur"].str[11:].tolist() == ["00:00", "01:00", "02:00", "02:00", "03:00"]


def test_api_times_follow_utc_through_the_repeated_hour():
    slots = hours_of(WINTER_DAY)
    df = pd.DataFrame({
        "time": pd.to_datetime([dt.datetime.fromtimestamp(ts) for ts in slots]),
        "slot": slots,
    })
    stamps = make_report().api_time_ts(df)
    assert stamps.is_monotonic_increasing and stamps.is_unique
    assert [str(s) for s in stamps[2:4]] == ["2026-10-25 02:00:00+02:00", "2026-10-25 02:00:00+01:00"]
    # without UTC times the repeated hour can't be told apart, but the API keeps answering
    fallback = make_report().api_time_ts(df.drop(columns=["slot"]))
    assert fallback.notna().all()
