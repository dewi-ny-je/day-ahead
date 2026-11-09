import datetime as dt
import logging
from types import SimpleNamespace

from dao.prog.day_ahead import DaCalc
from dao.prog.utils import parse_window_state


class _Calc:
    _read_planned_datetime = DaCalc._read_planned_datetime
    _write_planned_datetime = DaCalc._write_planned_datetime
    _read_machine_window = DaCalc._read_machine_window
    _reset_instant_start = DaCalc._reset_instant_start
    _planned_program_index = DaCalc._planned_program_index
    _write_planned_program = DaCalc._write_planned_program

    def __init__(self, states=None, fail_write=False, machines=None):
        self.states = states or {}
        self.fail_write = fail_write
        self.written = []
        self.turned_off = []
        self.machines = machines or []

    def set_value(self, entity_id, value):
        self.call_service("set_value", entity_id=entity_id, value=value)

    def select_option(self, entity_id, option):
        self.call_service("select_option", entity_id=entity_id, option=option)

    def turn_off(self, entity_id):
        if self.fail_write:
            raise ValueError("service call failed")
        self.turned_off.append(entity_id)

    def get_state(self, entity_id):
        if entity_id not in self.states:
            raise ValueError(f"Entity {entity_id} not found")
        return SimpleNamespace(state=self.states[entity_id])

    def call_service(self, service, **kwargs):
        if self.fail_write:
            raise ValueError("service call failed")
        self.written.append((service, kwargs))


def test_reads_the_stored_datetime():
    calc = _Calc({"input_datetime.wm_start": "2026-11-05 14:15:00"})

    result = calc._read_planned_datetime("input_datetime.wm_start", "wm")

    assert result == dt.datetime(2026, 11, 5, 14, 15)


def test_no_entity_configured_returns_none_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert _Calc()._read_planned_datetime(None, "wm") is None
    assert caplog.records == []


def test_missing_entity_warns_and_returns_none(caplog):
    with caplog.at_level(logging.WARNING):
        result = _Calc()._read_planned_datetime("input_datetime.wm_start", "wm")

    assert result is None
    assert "input_datetime.wm_start" in caplog.text


def test_unparsable_state_warns_and_returns_none(caplog):
    calc = _Calc({"input_datetime.wm_start": "unknown"})

    with caplog.at_level(logging.WARNING):
        result = calc._read_planned_datetime("input_datetime.wm_start", "wm")

    assert result is None
    assert "unknown" in caplog.text


def test_write_calls_set_datetime():
    calc = _Calc()

    assert calc._write_planned_datetime(
        "input_datetime.wm_end", "2026-11-05 16:00", "wm"
    )
    assert calc.written == [
        (
            "set_datetime",
            {"entity_id": "input_datetime.wm_end", "datetime": "2026-11-05 16:00"},
        )
    ]


def test_failed_write_warns_and_returns_false(caplog):
    calc = _Calc(fail_write=True)

    with caplog.at_level(logging.WARNING):
        ok = calc._write_planned_datetime(
            "input_datetime.wm_end", "2026-11-05 16:00", "wm"
        )

    assert not ok
    assert "input_datetime.wm_end" in caplog.text


RUN = dt.timedelta(hours=2)
START = dt.datetime(2026, 11, 5, 14, 0)


def test_previous_plan_uses_both_stored_times():
    end = START + dt.timedelta(hours=3)
    assert DaCalc._previous_plan(START, end, RUN, "wm") == (START, end)


def test_previous_plan_without_times_is_none():
    assert DaCalc._previous_plan(None, None, RUN, "wm") is None


def test_previous_plan_derives_missing_end_or_start():
    assert DaCalc._previous_plan(START, None, RUN, "wm") == (START, START + RUN)
    assert DaCalc._previous_plan(None, START, RUN, "wm") == (START - RUN, START)


def test_previous_plan_ignores_an_end_not_after_the_start(caplog):
    stale_end = START - dt.timedelta(days=1)

    with caplog.at_level(logging.WARNING):
        result = DaCalc._previous_plan(START, stale_end, RUN, "wm")

    assert result == (START, START + RUN)
    assert "wm" in caplog.text


NOW = dt.datetime(2026, 11, 9, 10, 0)


def test_window_state_time_of_day_lies_on_the_day_of_now():
    assert parse_window_state("18:00:00", NOW) == (dt.datetime(2026, 11, 9, 18), False)
    assert parse_window_state("08:30", NOW) == (
        dt.datetime(2026, 11, 9, 8, 30),
        False,
    )


def test_window_state_with_date_is_used_as_is():
    assert parse_window_state("2026-11-10 18:00:00", NOW) == (
        dt.datetime(2026, 11, 10, 18),
        True,
    )


def test_window_state_with_offset_is_converted_to_local_time():
    aware = dt.datetime(2026, 11, 10, 17, tzinfo=dt.UTC)
    expected = aware.astimezone().replace(tzinfo=None)

    assert parse_window_state("2026-11-10T17:00:00+00:00", NOW) == (expected, True)


def test_window_state_unknown_raises_value_error():
    for state in ("unknown", "unavailable", ""):
        try:
            parse_window_state(state, NOW)
        except ValueError:
            continue
        raise AssertionError(f"{state!r} was accepted")


def test_machine_window_reads_both_entities():
    calc = _Calc(
        {
            "input_datetime.wm_from": "2026-11-10 18:00:00",
            "input_datetime.wm_until": "2026-11-11 07:00:00",
        }
    )

    assert calc._read_machine_window(
        "input_datetime.wm_from", "input_datetime.wm_until", NOW, "wm"
    ) == (dt.datetime(2026, 11, 10, 18), dt.datetime(2026, 11, 11, 7), True)


def test_machine_window_times_only_has_no_date():
    calc = _Calc(
        {"input_datetime.wm_from": "18:00:00", "input_datetime.wm_until": "07:00:00"}
    )

    assert calc._read_machine_window(
        "input_datetime.wm_from", "input_datetime.wm_until", NOW, "wm"
    ) == (dt.datetime(2026, 11, 9, 18), dt.datetime(2026, 11, 9, 7), False)


def test_machine_window_with_unreadable_state_is_none(caplog):
    calc = _Calc(
        {"input_datetime.wm_from": "unavailable", "input_datetime.wm_until": "07:00:00"}
    )

    with caplog.at_level(logging.ERROR):
        result = calc._read_machine_window(
            "input_datetime.wm_from", "input_datetime.wm_until", NOW, "wm"
        )

    assert result is None
    assert "input_datetime.wm_from" in caplog.text


def test_machine_window_without_entity_is_none(caplog):
    calc = _Calc({"input_datetime.wm_until": "07:00:00"})

    with caplog.at_level(logging.ERROR):
        result = calc._read_machine_window(None, "input_datetime.wm_until", NOW, "wm")

    assert result is None
    assert "entity start window" in caplog.text


def test_machine_window_with_date_ending_before_start_is_none(caplog):
    calc = _Calc(
        {
            "input_datetime.wm_from": "2026-11-10 18:00:00",
            "input_datetime.wm_until": "2026-11-10 07:00:00",
        }
    )

    with caplog.at_level(logging.ERROR):
        result = calc._read_machine_window(
            "input_datetime.wm_from", "input_datetime.wm_until", NOW, "wm"
        )

    assert result is None


def _machine(instant_entity):
    return SimpleNamespace(name="wm", entity_instant_start=instant_entity)


def test_reset_instant_start_turns_the_entity_off():
    calc = _Calc(machines=[_machine("input_boolean.wm_now")])

    calc._reset_instant_start(0)

    assert calc.turned_off == ["input_boolean.wm_now"]


def test_failed_reset_of_instant_start_warns(caplog):
    calc = _Calc(fail_write=True, machines=[_machine("input_boolean.wm_now")])

    with caplog.at_level(logging.WARNING):
        calc._reset_instant_start(0)

    assert "input_boolean.wm_now" in caplog.text


PROGRAMS = [
    SimpleNamespace(name="off", power=[]),
    SimpleNamespace(name="eco", power=[2000, 500]),
]


def _program_machine(program_entity):
    return SimpleNamespace(
        name="wm", programs=PROGRAMS, entity_calculated_program=program_entity
    )


def test_planned_program_without_entity_is_the_selected_one():
    calc = _Calc(machines=[_program_machine(None)])

    assert calc._planned_program_index(0, 0) == 0


def test_planned_program_is_read_from_the_entity():
    calc = _Calc(
        {"input_text.wm_program": "eco"},
        machines=[_program_machine("input_text.wm_program")],
    )

    assert calc._planned_program_index(0, 0) == 1


def test_unknown_planned_program_falls_back_to_the_selected_one(caplog):
    calc = _Calc(
        {"input_text.wm_program": "unknown"},
        machines=[_program_machine("input_text.wm_program")],
    )

    with caplog.at_level(logging.WARNING):
        assert calc._planned_program_index(0, 1) == 1
    assert "input_text.wm_program" in caplog.text


def test_missing_planned_program_entity_falls_back_to_the_selected_one(caplog):
    calc = _Calc(machines=[_program_machine("input_text.wm_program")])

    with caplog.at_level(logging.WARNING):
        assert calc._planned_program_index(0, 1) == 1
    assert "input_text.wm_program" in caplog.text


def test_planned_program_is_written_to_an_input_text():
    calc = _Calc(machines=[_program_machine("input_text.wm_program")])

    calc._write_planned_program(0, "eco")

    assert calc.written == [
        ("set_value", {"entity_id": "input_text.wm_program", "value": "eco"})
    ]


def test_planned_program_is_selected_in_an_input_select():
    calc = _Calc(machines=[_program_machine("input_select.wm_program")])

    calc._write_planned_program(0, "eco")

    assert calc.written == [
        ("select_option", {"entity_id": "input_select.wm_program", "option": "eco"})
    ]


def test_failed_write_of_planned_program_warns(caplog):
    calc = _Calc(fail_write=True, machines=[_program_machine("input_text.wm_program")])

    with caplog.at_level(logging.WARNING):
        calc._write_planned_program(0, "eco")

    assert "input_text.wm_program" in caplog.text
