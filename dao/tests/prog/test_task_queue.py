"""The task queue with real worker processes: order, restart, join and cleanup."""

import json
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dao.prog import da_task_queue as q

REPO = Path(__file__).resolve().parents[3]

WORKER = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, {repo!r})
    from dao.prog import da_task_queue as q

    task, policy, duration, marker, protect = sys.argv[1:6]
    code = int(sys.argv[6]) if len(sys.argv) > 6 else 0
    task_def = {{"on_duplicate": policy}}
    if os.environ.get("TEST_TASK_TIMEOUT"):
        q.TASK_TIMEOUT = int(os.environ["TEST_TASK_TIMEOUT"])
    job_id = q.run_gate(task, [], task_def)
    with open(marker, "a") as f:
        f.write(f"start {{task}} {{job_id}} {{time.time()}}\\n")
    if protect == "1":
        q.protect_output()
    time.sleep(float(duration))
    with open(marker, "a") as f:
        f.write(f"end {{task}} {{job_id}} {{time.time()}}\\n")
    q.finish(job_id, code)
    sys.exit(code)
    """
)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv(q.ENV_QUEUE_DIR, str(tmp_path / "queue"))
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER.format(repo=str(REPO)))
    marker = tmp_path / "marker.txt"
    marker.touch()
    procs = []

    def start(task, policy="join", duration=0.0, protect=False, code=0):
        cmd = [sys.executable, str(worker), task, policy, str(duration), str(marker),
               "1" if protect else "0", str(code)]
        proc, job_id, entry = q.spawn(cmd, source="test", stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True)
        assert entry is not None, proc.communicate()[0]
        procs.append(proc)
        return proc, job_id

    def events():
        return [line.split()[:3] for line in marker.read_text().splitlines()]

    def wait_for(predicate, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError("condition not reached")

    yield start, events, wait_for
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def _finish_all(*procs, timeout=30):
    return [p.communicate(timeout=timeout)[0] for p in procs]


def test_different_tasks_run_one_at_a_time_in_arrival_order(queue):
    start, events, _wait_for = queue
    a, id_a = start("meteo", duration=1)
    b, id_b = start("prices", duration=0.2)
    c, id_c = start("tibber")

    assert q.get(id_b)["position"] == 1
    assert q.get(id_c)["position"] == 2
    _finish_all(a, b, c)

    assert events() == [
        ["start", "meteo", id_a], ["end", "meteo", id_a],
        ["start", "prices", id_b], ["end", "prices", id_b],
        ["start", "tibber", id_c], ["end", "tibber", id_c],
    ]
    assert [a.returncode, b.returncode, c.returncode] == [0, 0, 0]
    assert {q.final_state(i) for i in (id_a, id_b, id_c)} == {q.DONE}


def test_hanging_task_is_stopped_after_the_timeout_and_the_queue_moves_on(queue, monkeypatch):
    start, events, _wait_for = queue
    monkeypatch.setenv("TEST_TASK_TIMEOUT", "1")
    hanging, id_hanging = start("meteo", duration=60)
    nxt, id_next = start("prices")

    _finish_all(hanging, nxt)

    assert hanging.returncode == -signal.SIGALRM
    assert q.final_state(id_hanging) == q.TIMEOUT
    assert q.final_state(id_next) == q.DONE
    assert events() == [
        ["start", "meteo", id_hanging],
        ["start", "prices", id_next], ["end", "prices", id_next],
    ]


def test_restart_stops_the_running_task_and_runs_the_new_one_next(queue):
    start, events, wait_for = queue
    old, id_old = start("calc", "restart", duration=60)
    wait_for(lambda: ["start", "calc", id_old] in events())
    other, id_other = start("meteo")
    new, id_new = start("calc", "restart")

    _finish_all(old, new, other)

    assert old.returncode == -signal.SIGTERM
    assert q.final_state(id_old) == q.SUPERSEDED
    assert q.get(id_old)["superseded_by"] == id_new
    # the repeat goes ahead of the task that was already waiting
    assert [e[:3] for e in events()] == [
        ["start", "calc", id_old],
        ["start", "calc", id_new], ["end", "calc", id_new],
        ["start", "meteo", id_other], ["end", "meteo", id_other],
    ]


def test_restart_replaces_a_queued_request_in_its_place(queue):
    start, events, _wait_for = queue
    busy, _ = start("meteo", duration=1.5)
    first, id_first = start("calc", "restart")
    later, _ = start("prices")
    second, id_second = start("calc", "restart")

    out_first = _finish_all(first)[0]
    assert first.returncode == q.EXIT_SUPERSEDED
    assert "vervangen" in out_first
    assert q.get(id_second)["position"] == 1
    _finish_all(busy, later, second)

    assert [e[1] for e in events() if e[0] == "start"] == ["meteo", "calc", "prices"]
    assert ["start", "calc", id_second] in events()
    assert ["start", "calc", id_first] not in events()


def test_join_waits_for_the_existing_run_and_takes_its_result(queue):
    start, events, _wait_for = queue
    first, id_first = start("train", duration=1)
    joined, id_joined = start("train")

    assert q.get(id_joined)["state"] == q.JOINED
    out = _finish_all(first, joined)[1]

    assert joined.returncode == 0
    assert "wacht op het resultaat" in out
    assert q.final_state(id_joined) == q.DONE
    assert [e for e in events() if e[0] == "start"] == [["start", "train", id_first]]


def test_join_reports_the_failure_of_the_existing_run(queue):
    start, _events, _wait_for = queue
    first, id_first = start("train", duration=1, code=1)
    joined, id_joined = start("train")
    _finish_all(first, joined)

    assert first.returncode == 1
    assert joined.returncode == 1
    assert q.final_state(id_first) == q.ERROR
    assert q.final_state(id_joined) == q.ERROR


def test_history_keeps_the_entry_a_joined_request_waits_for(tmp_path, monkeypatch):
    monkeypatch.setenv(q.ENV_QUEUE_DIR, str(tmp_path))
    awaited = {"id": "target", "key": "x", "task": "x", "pid": 1, "state": "done",
               "finished": 0.0, "exit_code": 0}
    newer = [{"id": f"j{i}", "key": "y", "task": "y", "pid": 1, "state": "done",
              "finished": float(i + 1)} for i in range(q.HISTORY_SIZE + 5)]
    joined = {"id": "joiner", "key": "x", "task": "x", "pid": 1, "state": "joined",
              "joined_to": "target"}
    (tmp_path / "task_queue.json").write_text(json.dumps({"jobs": [awaited, *newer, joined]}))
    monkeypatch.setattr(q, "_alive", lambda entry: True)

    assert q.wait_turn("joiner", poll=0.01)["state"] == q.DONE


def test_queue_dir_becomes_the_add_on_data_link(tmp_path, monkeypatch):
    monkeypatch.delenv(q.ENV_QUEUE_DIR, raising=False)
    ha_data = tmp_path / "config" / "dao_data"
    ha_data.mkdir(parents=True)
    monkeypatch.setattr(q, "HA_DATA_DIR", str(ha_data))
    data = tmp_path / "dao" / "data"
    data.parent.mkdir()

    q._ensure_dir(data)

    assert data.is_symlink() and data.resolve() == ha_data.resolve()


def test_stop_during_protected_output_lets_the_task_finish(queue):
    start, events, wait_for = queue
    old, id_old = start("calc", "restart", duration=1.5, protect=True)
    wait_for(lambda: ["start", "calc", id_old] in events())
    new, _ = start("calc", "restart")
    out_old, _ = _finish_all(old, new)

    assert old.returncode == 0
    assert "eerst volledig weggeschreven" in out_old
    assert q.final_state(id_old) == q.DONE
    assert [e[0] for e in events()] == ["start", "end", "start", "end"]


def test_cancel_a_queued_and_a_running_task(queue):
    start, events, wait_for = queue
    running, id_running = start("meteo", duration=60)
    wait_for(lambda: ["start", "meteo", id_running] in events())
    waiting, id_waiting = start("prices")

    assert q.cancel(id_waiting)
    assert _finish_all(waiting) and waiting.returncode == q.EXIT_CANCELLED
    assert q.cancel(id_running)
    _finish_all(running)

    assert q.final_state(id_waiting) == q.CANCELLED
    assert q.final_state(id_running) == q.CANCELLED
    assert not q.cancel(id_running)


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.mark.parametrize("entry,expected", [
    ({"state": "running"}, "error"),
    ({"state": "running", "stop_reason": "superseded"}, "superseded"),
    ({"state": "running", "deadline": 1.0}, "timeout"),
    ({"state": "queued", "seq": 1}, "error"),
])
def test_entries_of_ended_processes_are_settled(tmp_path, monkeypatch, entry, expected):
    monkeypatch.setenv(q.ENV_QUEUE_DIR, str(tmp_path))
    entry = {"id": "gone", "key": "x", "task": "x", "pid": _dead_pid(), **entry}
    (tmp_path / "task_queue.json").write_text(json.dumps({"jobs": [entry]}))

    assert q.final_state("gone") == expected
    # a queue blocked by nothing: a new request may run straight away
    job = q.enrol("meteo", [], job_id="next")
    assert q.wait_turn(job["id"], poll=0.01)["state"] == q.RUNNING


def test_history_keeps_the_newest_finished_entries(tmp_path, monkeypatch):
    monkeypatch.setenv(q.ENV_QUEUE_DIR, str(tmp_path))
    old = [{"id": f"j{i}", "key": "x", "task": "x", "pid": 1, "state": "done",
            "finished": float(i)} for i in range(q.HISTORY_SIZE + 5)]
    (tmp_path / "task_queue.json").write_text(json.dumps({"jobs": old}))

    ids = [j["id"] for j in q.jobs()]
    assert len(ids) == q.HISTORY_SIZE
    assert ids[0] == f"j{q.HISTORY_SIZE + 4}"
    assert "j0" not in ids


def test_unreadable_queue_file_starts_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(q.ENV_QUEUE_DIR, str(tmp_path))
    (tmp_path / "task_queue.json").write_text("not json")
    assert q.jobs() == []


@pytest.mark.parametrize("task,args,task_def,expected", [
    ("calc_optimum", [], {"queue_group": "calc"}, "calc"),
    ("calc_optimum_met_debug", ["--debug"], {"queue_group": "calc"}, "calc"),
    ("get_day_ahead_prices", ["2026-11-02"], {}, "get_day_ahead_prices 2026-11-02"),
    ("get_meteo_data", [], None, "get_meteo_data"),
])
def test_job_key(task, args, task_def, expected):
    assert q.job_key(task, args, task_def) == expected


def test_calc_tasks_restart_and_others_join():
    from dao.prog.da_const import tasks

    assert q.duplicate_policy(tasks["calc_optimum"]) == q.RESTART
    assert q.duplicate_policy(tasks["calc_optimum_met_debug"]) == q.RESTART
    assert q.job_key("calc_optimum", [], tasks["calc_optimum"]) == q.job_key(
        "calc_optimum_met_debug", ["--debug"], tasks["calc_optimum_met_debug"])
    others = [name for name in tasks if not name.startswith("calc_optimum")]
    assert {q.duplicate_policy(tasks[name]) for name in others} == {q.JOIN}


SLOW_DAY_AHEAD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {repo!r})
    time.sleep(1)  # loading the modules takes a while
    from dao.prog import da_task_queue as q
    from dao.prog.da_const import tasks

    job_id = q.run_gate("calc_optimum", sys.argv[2:], tasks["calc_optimum"])
    with open({marker!r}, "a") as f:
        f.write(f"start calc_optimum {{job_id}}\\n")
    q.finish(job_id, 0)
    """
)


def test_spawn_enrols_a_day_ahead_task_before_it_has_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv(q.ENV_QUEUE_DIR, str(tmp_path / "queue"))
    marker = tmp_path / "marker.txt"
    script_dir = tmp_path / "prog"
    script_dir.mkdir()
    (script_dir / "day_ahead.py").write_text(
        SLOW_DAY_AHEAD.format(repo=str(REPO), marker=str(marker)))
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER.format(repo=str(REPO)))

    _calc, id_calc, entry = q.spawn([sys.executable, str(script_dir / "day_ahead.py"), "calc"],
                                   source="test")
    assert entry["state"] == q.QUEUED and entry["task"] == "calc_optimum"
    _meteo, id_meteo, _ = q.spawn(
        [sys.executable, str(worker), "meteo", "join", "0", str(marker), "0"], source="test")

    end = time.time() + 30
    while q.final_state(id_meteo) not in q.FINISHED_STATES and time.time() < end:
        time.sleep(0.1)
    starts = [line.split() for line in marker.read_text().splitlines()]
    assert [words[1] for words in starts if words[0] == "start"] == [
        "calc_optimum", "meteo"]
    assert q.final_state(id_calc) == q.DONE


def test_task_from_cmd_resolves_aliases():
    assert q.task_from_cmd(["python3", "day_ahead.py", "calc", "--debug"])[:2] == (
        "calc_optimum", ["--debug"])
    assert q.task_from_cmd(["python3", "../prog/day_ahead.py", "prices", "2026-11-02"])[:2] == (
        "get_day_ahead_prices", ["2026-11-02"])
    assert q.task_from_cmd(["python3", "day_ahead.py", "unknown"]) is None
    assert q.task_from_cmd(["python3", "other.py", "calc"]) is None


def test_day_ahead_waits_for_its_turn_before_connecting(monkeypatch):
    pytest.importorskip("mip")
    from dao.prog import day_ahead

    calls = []
    monkeypatch.setattr(sys, "argv", ["day_ahead.py", "calc", "--debug"])
    monkeypatch.setattr(day_ahead.da_task_queue, "run_gate",
                        lambda task, args, task_def: calls.append(("gate", task, args)) or "job1")
    monkeypatch.setattr(day_ahead.da_task_queue, "finish",
                        lambda job_id, code: calls.append(("finish", job_id, code)))

    def fake_calc(path):
        calls.append(("connect",))
        return type("Calc", (), {"config": None})()

    monkeypatch.setattr(day_ahead, "DaCalc", fake_calc)
    monkeypatch.setattr(day_ahead.os, "_exit", lambda code: calls.append(("exit", code)))

    day_ahead.main()

    assert calls == [("gate", "calc_optimum", ["--debug"]), ("connect",),
                     ("finish", "job1", 1), ("exit", 1)]
