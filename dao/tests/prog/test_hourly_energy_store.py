"""Hourly energy kept in DAO's database for entities without HA statistics."""

import datetime as dt
import json
import threading

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
)

from dao.lib.db_manager import DBmanagerObj
from dao.prog.da_report import Report
from dao.prog.hourly_energy_store import (
    HOUR,
    TABLE_NAME,
    HourlyEnergyStore,
    hourly_energy_from_readings,
)

T0 = 1_700_000_000 - 1_700_000_000 % HOUR  # a whole hour


def manager() -> DBmanagerObj:
    db = DBmanagerObj.__new__(DBmanagerObj)
    db.db_dialect = "sqlite"
    db.engine = create_engine("sqlite:///:memory:")
    db.metadata = MetaData()
    db._metadata_lock = threading.RLock()
    return db


class FakeHA:
    """The recorder tables of Home Assistant that the store reads."""

    def __init__(self):
        self.db = manager()
        md = MetaData()
        self.meta = Table(
            "states_meta", md,
            Column("metadata_id", Integer, primary_key=True),
            Column("entity_id", String),
        )
        self.attrs = Table(
            "state_attributes", md,
            Column("attributes_id", Integer, primary_key=True),
            Column("shared_attrs", String),
        )
        self.states = Table(
            "states", md,
            Column("state_id", Integer, primary_key=True),
            Column("state", String),
            Column("attributes_id", Integer),
            Column("last_updated_ts", Float),
            Column("metadata_id", Integer),
        )
        self.stat_meta = Table(
            "statistics_meta", md,
            Column("id", Integer, primary_key=True),
            Column("statistic_id", String),
            Column("has_sum", Boolean),
        )
        md.create_all(self.db.engine)
        self.ids = {}

    def add_statistics(self, entity):
        with self.db.engine.begin() as c:
            c.execute(self.stat_meta.insert(), {"statistic_id": entity, "has_sum": True})

    def add_states(self, entity, unit, rows):
        with self.db.engine.begin() as c:
            if entity not in self.ids:
                self.ids[entity] = len(self.ids) + 1
                c.execute(self.meta.insert(), {"metadata_id": self.ids[entity], "entity_id": entity})
                c.execute(self.attrs.insert(), {
                    "attributes_id": self.ids[entity],
                    "shared_attrs": json.dumps({"unit_of_measurement": unit}),
                })
            c.execute(self.states.insert(), [
                {"state": str(state), "attributes_id": self.ids[entity],
                 "last_updated_ts": float(ts), "metadata_id": self.ids[entity]}
                for ts, state in rows
            ])


@pytest.fixture
def ha():
    return FakeHA()


@pytest.fixture
def store(ha):
    return HourlyEnergyStore(manager(), ha.db)


def stored(store, entity):
    table = store.table()
    with store.db_da.engine.connect() as c:
        rows = c.execute(
            select(table.c.time, table.c.value)
            .where(table.c.entity_id == entity)
            .order_by(table.c.time)
        ).all()
    return {int(t): round(v, 6) for t, v in rows}


# ----------------------------------------------------------------------
def test_readings_to_hours():
    readings = [(T0 + 600, 10.0), (T0 + HOUR + 60, 11.5), (T0 + HOUR + 1800, 12.0)]
    result = hourly_energy_from_readings(readings, T0, T0 + 4 * HOUR, reference=9.0)
    assert result == {T0: 1.0, T0 + HOUR: 2.0, T0 + 2 * HOUR: 0.0, T0 + 3 * HOUR: 0.0}


def test_hour_of_first_reading_is_left_out():
    readings = [(T0 + 600, 10.0), (T0 + HOUR + 60, 11.0)]
    result = hourly_energy_from_readings(readings, T0, T0 + 2 * HOUR)
    assert result == {T0 + HOUR: 1.0}


def test_reset_counts_the_new_reading():
    # nightly reset: 25 -> 0.4 means 0.4 kWh used since the reset
    readings = [(T0 + 600, 25.0), (T0 + 1200, 0.4), (T0 + 1800, 1.0)]
    result = hourly_energy_from_readings(readings, T0, T0 + HOUR, reference=24.0)
    assert result == {T0: pytest.approx(1.0 + 0.4 + 0.6)}


def test_small_dip_is_ignored():
    readings = [(T0 + 600, 100.0), (T0 + 1200, 99.5), (T0 + 1800, 101.0)]
    result = hourly_energy_from_readings(readings, T0, T0 + HOUR, reference=99.0)
    assert result == {T0: pytest.approx(2.0)}


# ----------------------------------------------------------------------
def test_sync_stores_only_entities_without_statistics(ha, store):
    ha.add_statistics("sensor.with_stats")
    ha.add_states("sensor.with_stats", "kWh", [(T0 - 60, 1), (T0 + 60, 2)])
    ha.add_states("sensor.no_stats", "kWh", [(T0 - 60, 1), (T0 + 60, 3), (T0 + HOUR + 60, 4)])
    now = dt.datetime.fromtimestamp(T0 + 2 * HOUR + 120)
    result = store.sync(["sensor.with_stats", "sensor.no_stats"], keep_days=1, now=now)
    assert result == {"sensor.no_stats": 2}
    assert stored(store, "sensor.with_stats") == {}
    assert stored(store, "sensor.no_stats") == {T0: 2.0, T0 + HOUR: 1.0}


def test_sync_converts_wh_and_skips_unavailable(ha, store):
    ha.add_states("sensor.wh", "Wh", [
        (T0 - 60, 1000), (T0 + 60, "unavailable"), (T0 + 120, 2500),
    ])
    store.sync(["sensor.wh"], keep_days=1, now=dt.datetime.fromtimestamp(T0 + HOUR))
    assert stored(store, "sensor.wh") == {T0: 1.5}


def test_sync_continues_after_the_last_stored_hour(ha, store):
    ha.add_states("sensor.e", "kWh", [(T0 - 60, 1), (T0 + 60, 2)])
    store.sync(["sensor.e"], keep_days=1, now=dt.datetime.fromtimestamp(T0 + HOUR + 10))
    ha.add_states("sensor.e", "kWh", [(T0 + HOUR + 60, 5)])
    result = store.sync(["sensor.e"], keep_days=1, now=dt.datetime.fromtimestamp(T0 + 2 * HOUR + 10))
    assert result == {"sensor.e": 1}
    assert stored(store, "sensor.e") == {T0: 1.0, T0 + HOUR: 3.0}


def test_stored_hours_survive_the_recorder_purge(ha, store):
    ha.add_states("sensor.e", "kWh", [(T0 - 60, 1), (T0 + 60, 2)])
    store.sync(["sensor.e"], keep_days=3, now=dt.datetime.fromtimestamp(T0 + HOUR + 10))
    with ha.db.engine.begin() as c:
        c.execute(ha.states.delete())
    store.sync(["sensor.e"], keep_days=3, now=dt.datetime.fromtimestamp(T0 + 2 * HOUR + 10))
    assert stored(store, "sensor.e")[T0] == 1.0


def test_sync_deletes_rows_older_than_keep_days(ha, store):
    ha.add_states("sensor.e", "kWh", [(T0 - 60, 1), (T0 + 60, 2)])
    store.sync(["sensor.e"], keep_days=1, now=dt.datetime.fromtimestamp(T0 + HOUR + 10))
    store.sync(["sensor.e"], keep_days=1, now=dt.datetime.fromtimestamp(T0 + 30 * HOUR))
    assert T0 not in stored(store, "sensor.e")


def test_get_hourly_returns_local_hours(ha, store):
    ha.add_states("sensor.e", "kWh", [(T0 - 60, 1), (T0 + 60, 2), (T0 + HOUR + 60, 4)])
    store.sync(["sensor.e"], keep_days=1, now=dt.datetime.fromtimestamp(T0 + 2 * HOUR))
    df = store.get_hourly(
        "sensor.e", dt.datetime.fromtimestamp(T0 + HOUR), dt.datetime.fromtimestamp(T0 + 2 * HOUR)
    )
    assert df["utc"].tolist() == [T0 + HOUR]
    assert df["tijd"].tolist() == [dt.datetime.fromtimestamp(T0 + HOUR)]
    assert df["value"].tolist() == [2.0]


# ----------------------------------------------------------------------
def test_baseload_week_data_reads_the_stored_hours(ha, store):
    report = Report.__new__(Report)
    report._hourly_energy_store = store
    ha.add_states("sensor.e", "kWh", [(T0 - 60, 1), (T0 + 60, 2), (T0 + HOUR + 60, 4)])
    store.sync(["sensor.e"], keep_days=1, now=dt.datetime.fromtimestamp(T0 + 2 * HOUR))
    first = dt.datetime.fromtimestamp(T0)
    df = report.get_sensor_week_data(
        "sensor.e", first.weekday(), first - dt.timedelta(days=1),
        first + dt.timedelta(days=1), "wp_consumption",
    )
    expected = {dt.datetime.fromtimestamp(T0 + h * HOUR): v for h, v in [(0, 1.0), (1, 2.0)]
                if dt.datetime.fromtimestamp(T0 + h * HOUR).weekday() == first.weekday()}
    assert dict(zip(df["tijd"], df["wp_consumption"], strict=True)) == expected
    assert df["uur"].tolist() == [t.hour for t in expected]
    other_day = (first.weekday() + 3) % 7
    assert len(report.get_sensor_week_data(
        "sensor.e", other_day, first - dt.timedelta(days=1),
        first + dt.timedelta(days=1), "wp_consumption",
    )) == 0


def test_table_is_created_in_the_dao_database(store):
    store.table()
    from sqlalchemy import inspect

    assert inspect(store.db_da.engine).has_table(TABLE_NAME)


def test_check_db_creates_the_table_once(capsys, monkeypatch):
    from pathlib import Path

    from sqlalchemy import inspect

    # check_db imports utils and version as top-level modules
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "prog"))
    from dao.prog.check_db import CheckDB

    check = CheckDB.__new__(CheckDB)
    check.db_da = manager()
    check.engine = check.db_da.engine
    check.ensure_hourly_energy_table()
    check.ensure_hourly_energy_table()
    assert inspect(check.engine).has_table(TABLE_NAME)
    assert capsys.readouterr().out.count("gecreeerd") == 1
