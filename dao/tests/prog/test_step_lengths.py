"""Step helpers for a horizon of quarters followed by an hourly prediction tail."""

import datetime

from dao.prog.utils import calc_uur_index, steps_covering_hours, steps_until_cooled


def _tijd(quarters: int, hours: int) -> list[datetime.datetime]:
    start = datetime.datetime(2026, 11, 3, 22, 0)
    tijd = [start + datetime.timedelta(minutes=15 * q) for q in range(quarters)]
    tail_start = start + datetime.timedelta(minutes=15 * quarters)
    tijd += [tail_start + datetime.timedelta(hours=h) for h in range(hours)]
    return tijd


def test_steps_covering_hours_constant_steps_matches_ceil():
    step_h = [0.25] * 20
    assert steps_covering_hours(step_h, 0, 0) == 0
    assert steps_covering_hours(step_h, 0, 1.0) == 4
    assert steps_covering_hours(step_h, 0, 1.1) == 5
    assert steps_covering_hours(step_h, 3, 0.5) == 2


def test_steps_covering_hours_into_hourly_tail():
    # 8 quarters (2 h), then hours
    step_h = [0.25] * 8 + [1.0] * 10
    assert steps_covering_hours(step_h, 0, 3.0) == 9
    assert steps_covering_hours(step_h, 6, 2.0) == 4  # 0.5 h of quarters + 2 tail hours
    assert steps_covering_hours(step_h, 8, 2.5) == 3


def test_steps_covering_hours_past_the_end_uses_last_step():
    assert steps_covering_hours([0.25, 1.0], 0, 3.25) == 4


def test_steps_until_cooled():
    # 0.5 K/h, 4 quarters then 2 hours
    step_h = [0.25] * 4 + [1.0] * 2
    cum = [0.0]
    for h in step_h:
        cum.append(cum[-1] + 0.5 * h)
    assert steps_until_cooled(cum, -1) == 0
    assert steps_until_cooled(cum, 0.25) == 2
    assert steps_until_cooled(cum, 0.5) == 4
    assert steps_until_cooled(cum, 1.0) == 5
    assert steps_until_cooled(cum, 10) == 6
    assert steps_until_cooled([0.0, 0.0, 0.0], 0.1) == 2


def test_calc_uur_index_in_hourly_tail():
    tijd = _tijd(quarters=8, hours=3)  # quarters 22:00-23:45, hours 00:00, 01:00, 02:00
    at = datetime.datetime
    assert calc_uur_index(at(2026, 11, 3, 22, 15), tijd, "15min") == 1
    assert calc_uur_index(at(2026, 11, 3, 23, 45), tijd, "15min") == 7
    assert calc_uur_index(at(2026, 11, 4, 0, 30), tijd, "15min") == 8
    assert calc_uur_index(at(2026, 11, 4, 1, 45), tijd, "15min") == 9
    assert calc_uur_index(at(2026, 11, 4, 2, 15), tijd, "15min") == 11  # past the end
    assert calc_uur_index(at(2026, 11, 3, 21, 45), tijd, "15min") == 11  # before the start


def test_calc_uur_index_hourly():
    tijd = _tijd(quarters=0, hours=3)
    assert calc_uur_index(datetime.datetime(2026, 11, 3, 23, 30), tijd, "1hour") == 1
    assert calc_uur_index(datetime.datetime(2026, 11, 4, 0, 59), tijd, "1hour") == 2
    assert calc_uur_index(datetime.datetime(2026, 11, 4, 1, 0), tijd, "1hour") == 3
