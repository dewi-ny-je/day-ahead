"""Log files of runs started in the same second keep apart."""

import datetime as dt

from dao.prog.utils import unique_log_path


def test_runs_in_the_same_second_get_their_own_log(tmp_path):
    now = dt.datetime(2026, 11, 11, 10, 15, 30)
    prefix = str(tmp_path / "calc_")

    first = unique_log_path(prefix, now)
    second = unique_log_path(prefix, now)
    third = unique_log_path(prefix, now)

    assert first.endswith("calc_2026-11-11__10:15:30.log")
    assert second.endswith("calc_2026-11-11__10:15:30_2.log")
    assert third.endswith("calc_2026-11-11__10:15:30_3.log")
    assert all((tmp_path / name).exists() for name in (first, second, third))
