import datetime as dt
import logging
from types import SimpleNamespace

from dao.prog.day_ahead import DaCalc


class _Calc:
    _read_planned_datetime = DaCalc._read_planned_datetime
    _write_planned_datetime = DaCalc._write_planned_datetime

    def __init__(self, states=None, fail_write=False):
        self.states = states or {}
        self.fail_write = fail_write
        self.written = []

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
