"""
Local store of the hourly energy of Home Assistant entities.

Home Assistant keeps long-term statistics, which never expire, only for
entities with a ``state_class``. For an energy entity without one, the only
source is the recorder's raw ``states`` table, which Home Assistant purges
after ``purge_keep_days`` (10 days by default). The baseload calculation looks
back ``baseload_calc_periode`` days (56 by default), so this module turns the
raw states of such entities into hourly energy and keeps it in DAO's own
database, in the table ``hourly_energy``, for as long as the baseload needs it.

Use:

* ``HourlyEnergyStore.sync(entities, keep_days)`` records every complete hour
  since the last recorded one (or since ``keep_days`` ago) for the entities
  without long-term statistics, and deletes rows older than ``keep_days``.
  It has to run at least once every ``purge_keep_days`` days, otherwise the
  hours that Home Assistant has purged in between stay missing.
* ``HourlyEnergyStore.get_hourly(entity, vanaf, tot)`` returns the stored
  hourly energy in kWh.
* ``HourlyEnergyStore.has_statistics(entity)`` tells which of the two sources
  applies to an entity.
"""

import datetime
import json
import logging

import pandas as pd
from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    delete,
    func,
    insert,
    select,
)

TABLE_NAME = "hourly_energy"
HOUR = 3600

# A reading below this fraction of the previous one is a reset of the meter,
# a smaller drop is noise and is ignored; Home Assistant uses the same rule for
# ``total_increasing`` sensors.
RESET_FRACTION = 0.9

UNIT_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}

NON_NUMERIC_STATES = ("unknown", "unavailable", "", None)


def define_table(metadata: MetaData) -> Table:
    """
    Return the ``hourly_energy`` table in ``metadata``, defining it if needed.

    Each row holds the energy in kWh that ``entity_id`` measured during the
    hour that starts at ``time`` (unix timestamp, UTC).
    """
    table = metadata.tables.get(TABLE_NAME)
    if table is not None and len(table.columns) > 0:
        return table
    return Table(
        TABLE_NAME,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("entity_id", String(255), nullable=False),
        Column("time", BigInteger, nullable=False),
        Column("value", Float),
        UniqueConstraint("entity_id", "time"),
        sqlite_autoincrement=True,
        extend_existing=True,
    )


def to_float(state) -> float | None:
    """The numeric value of a raw HA state, None for unavailable or text."""
    if state in NON_NUMERIC_STATES:
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def hourly_energy_from_readings(
    readings: list[tuple[float, float]],
    start_ts: int,
    end_ts: int,
    reference: float | None = None,
) -> dict[int, float]:
    """
    Energy per hour from the readings of a cumulative energy meter.

    :param readings: (timestamp, reading) pairs sorted by time, with
        ``start_ts <= timestamp < end_ts``
    :param start_ts: start of the first hour (unix timestamp, whole hour)
    :param end_ts: end of the last hour (unix timestamp, whole hour)
    :param reference: the last reading before ``start_ts``, None if unknown
    :return: {hour start: energy in the unit of the readings}. An hour is only
        included when the reading at its start is known, so the hour in which
        the first reading arrives is left out. A reading that drops below
        ``RESET_FRACTION`` of the previous one is a reset: the new reading is
        the energy used since the reset. A smaller drop is ignored.
    """
    result = {}
    last = reference
    index = 0
    for hour in range(start_ts, end_ts, HOUR):
        known_at_start = last is not None
        energy = 0.0
        while index < len(readings) and readings[index][0] < hour + HOUR:
            value = readings[index][1]
            index += 1
            if last is None:
                last = value
            elif value >= last:
                energy += value - last
                last = value
            elif value < last * RESET_FRACTION:
                energy += max(value, 0.0)
                last = value
        if known_at_start:
            result[hour] = energy
    return result


class HourlyEnergyStore:
    def __init__(self, db_da, db_ha):
        """
        :param db_da: DBmanagerObj of the DAO database
        :param db_ha: DBmanagerObj of the Home Assistant database
        """
        self.db_da = db_da
        self.db_ha = db_ha
        self._table = None
        self._statistics_entities = None

    # ------------------------------------------------------------------
    # DAO database
    def table(self) -> Table:
        """The ``hourly_energy`` table, created when it does not exist yet."""
        if self._table is None:
            table = define_table(self.db_da.metadata)
            table.create(self.db_da.engine, checkfirst=True)
            self._table = table
        return self._table

    def last_time(self, entity: str) -> int | None:
        table = self.table()
        query = select(func.max(table.c.time)).where(table.c.entity_id == entity)
        with self.db_da.engine.connect() as connection:
            result = connection.execute(query).scalar()
        return None if result is None else int(result)

    def save(self, entity: str, energy: dict[int, float]) -> None:
        """Replace the stored hours of ``entity`` with ``energy`` (kWh)."""
        if not energy:
            return
        table = self.table()
        rows = [
            {"entity_id": entity, "time": int(ts), "value": round(value, 6)}
            for ts, value in sorted(energy.items())
        ]
        with self.db_da.engine.begin() as connection:
            connection.execute(
                delete(table).where(
                    and_(
                        table.c.entity_id == entity,
                        table.c.time >= rows[0]["time"],
                        table.c.time <= rows[-1]["time"],
                    )
                )
            )
            connection.execute(insert(table), rows)

    def prune(self, oldest_ts: int) -> None:
        """Delete every row of an hour that starts before ``oldest_ts``."""
        table = self.table()
        with self.db_da.engine.begin() as connection:
            connection.execute(delete(table).where(table.c.time < oldest_ts))

    def get_hourly(
        self, entity: str, vanaf: datetime.datetime, tot: datetime.datetime
    ) -> pd.DataFrame:
        """
        The stored hourly energy of ``entity`` in kWh.

        :param entity: entity id
        :param vanaf: first hour (local time), included
        :param tot: end (local time), excluded
        :return: DataFrame with the columns "tijd" (start of the hour, naive
            local time as datetime.fromtimestamp gives it), "utc" (unix
            timestamp of that start) and "value" (kWh), sorted by time. Hours
            that were never recorded are absent.
        """
        table = self.table()
        query = (
            select(table.c.time, table.c.value)
            .where(
                and_(
                    table.c.entity_id == entity,
                    table.c.time >= int(vanaf.timestamp()),
                    table.c.time < int(tot.timestamp()),
                )
            )
            .order_by(table.c.time)
        )
        with self.db_da.engine.connect() as connection:
            rows = connection.execute(query).all()
        return pd.DataFrame(
            {
                "tijd": [datetime.datetime.fromtimestamp(int(r[0])) for r in rows],
                "utc": [int(r[0]) for r in rows],
                "value": [float(r[1]) for r in rows],
            }
        )

    # ------------------------------------------------------------------
    # Home Assistant database
    def statistics_entities(self) -> set[str]:
        """The entities for which HA keeps long-term statistics with a sum."""
        if self._statistics_entities is None:
            meta = self.db_ha.get_table("statistics_meta")
            query = select(meta.c.statistic_id).where(meta.c.has_sum.is_(True))
            with self.db_ha.engine.connect() as connection:
                self._statistics_entities = {
                    row[0] for row in connection.execute(query)
                }
        return self._statistics_entities

    def has_statistics(self, entity: str) -> bool:
        return entity in self.statistics_entities()

    def entities_without_statistics(self, entities: list[str]) -> list[str]:
        """The entities of ``entities`` (without duplicates) to keep locally."""
        result = []
        for entity in entities:
            if entity and entity not in result and not self.has_statistics(entity):
                result.append(entity)
        return result

    def _states_query(self, entity: str):
        states = self.db_ha.get_table("states")
        states_meta = self.db_ha.get_table("states_meta")
        return states, select(states.c.last_updated_ts, states.c.state).select_from(
            states.join(states_meta, states.c.metadata_id == states_meta.c.metadata_id)
        ).where(states_meta.c.entity_id == entity)

    def read_states(
        self, entity: str, start_ts: int, end_ts: int
    ) -> tuple[float | None, list[tuple[float, float]]]:
        """
        The numeric raw states of ``entity`` from the HA recorder.

        :return: (the last numeric reading before ``start_ts`` or None,
            the numeric readings with ``start_ts <= time < end_ts``)
        """
        states, query = self._states_query(entity)
        in_range = query.where(
            and_(
                states.c.last_updated_ts >= start_ts,
                states.c.last_updated_ts < end_ts,
            )
        ).order_by(states.c.last_updated_ts)
        before = (
            query.where(states.c.last_updated_ts < start_ts)
            .order_by(states.c.last_updated_ts.desc())
            .limit(50)
        )
        with self.db_ha.engine.connect() as connection:
            readings = []
            for ts, state in connection.execute(in_range):
                value = to_float(state)
                if value is not None:
                    readings.append((float(ts), value))
            reference = None
            for _, state in connection.execute(before):
                reference = to_float(state)
                if reference is not None:
                    break
        return reference, readings

    def unit_factor(self, entity: str) -> float:
        """Factor from the entity's unit of measurement to kWh."""
        states = self.db_ha.get_table("states")
        states_meta = self.db_ha.get_table("states_meta")
        attributes = self.db_ha.get_table("state_attributes")
        query = (
            select(attributes.c.shared_attrs)
            .select_from(
                states.join(
                    states_meta, states.c.metadata_id == states_meta.c.metadata_id
                ).join(attributes, states.c.attributes_id == attributes.c.attributes_id)
            )
            .where(states_meta.c.entity_id == entity)
            .order_by(states.c.last_updated_ts.desc())
            .limit(1)
        )
        with self.db_ha.engine.connect() as connection:
            shared_attrs = connection.execute(query).scalar()
        unit = None
        if shared_attrs:
            try:
                unit = json.loads(shared_attrs).get("unit_of_measurement")
            except (TypeError, ValueError, AttributeError):
                unit = None
        if unit not in UNIT_FACTORS:
            logging.warning(
                f"Eenheid van {entity} is {unit!r}, geen Wh, kWh of MWh; "
                f"de waarden worden als kWh opgeslagen"
            )
            return 1.0
        return UNIT_FACTORS[unit]

    # ------------------------------------------------------------------
    def sync_entity(self, entity: str, oldest_ts: int, end_ts: int) -> int:
        """
        Record the complete hours of ``entity`` after the last recorded hour,
        from ``oldest_ts`` at the earliest, up to ``end_ts``.

        :return: the number of hours stored
        """
        last = self.last_time(entity)
        start_ts = oldest_ts if last is None else max(last + HOUR, oldest_ts)
        if start_ts >= end_ts:
            return 0
        reference, readings = self.read_states(entity, start_ts, end_ts)
        energy = hourly_energy_from_readings(readings, start_ts, end_ts, reference)
        if not energy:
            return 0
        factor = self.unit_factor(entity)
        self.save(entity, {ts: value * factor for ts, value in energy.items()})
        return len(energy)

    def sync(
        self,
        entities: list[str],
        keep_days: int,
        now: datetime.datetime | None = None,
    ) -> dict[str, int]:
        """
        Record the hourly energy of the entities without long-term statistics
        and delete the rows older than ``keep_days`` days.

        :param entities: entity ids; those with long-term statistics are skipped
        :param keep_days: number of days to keep
        :param now: the current time, for tests
        :return: {entity: number of hours stored}
        """
        now_ts = int((now or datetime.datetime.now()).timestamp())
        end_ts = now_ts - now_ts % HOUR
        oldest_ts = end_ts - keep_days * 24 * HOUR
        result = {}
        for entity in self.entities_without_statistics(entities):
            try:
                result[entity] = self.sync_entity(entity, oldest_ts, end_ts)
            except Exception as ex:
                logging.warning(f"Uurwaarden van {entity} niet opgeslagen: {ex}")
                result[entity] = 0
            logging.info(
                f"{entity} heeft geen langetermijnstatistiek in HA: "
                f"{result[entity]} uur opgeslagen in {TABLE_NAME}"
            )
        self.prune(oldest_ts)
        return result
