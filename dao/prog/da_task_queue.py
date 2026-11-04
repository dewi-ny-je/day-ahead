"""Shared queue for task runs.

Every task runs as ``python3 day_ahead.py <task>``, whether the scheduler,
the REST API, the web GUI or a shell starts it. Each of those processes
enrols here before it builds its connections, then waits for its turn, so
at most one task runs at a time and the others run in arrival order.

A request for a task that is already queued or running is handled by the
task's ``on_duplicate`` policy:

* ``restart``: the older entry is superseded. A queued one is dropped and
  the new request takes its place; a running one gets SIGTERM and the new
  request takes the head of the queue.
* ``join``: the new request starts nothing. It waits for the existing entry
  and ends with that entry's result.

A task that is stopped while it runs ends by the signal itself (SIGTERM
for a stop request, SIGALRM after TASK_TIMEOUT), so it stops at once, also
inside the solver; its exit status is then the signal's. Its final state
(superseded, cancelled, timeout) is in its queue entry. Exit codes 3 and 4
belong to requests that end before their run starts.

The queue is a JSON file next to ``options.json``, read and written under an
exclusive ``flock``. Every entry stores the PID of its process, and an
entry whose process has gone is settled the next time anyone opens the
queue, so a crash or a container restart cannot block it.
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ENV_JOB_ID = "DAO_JOB_ID"
ENV_JOB_SOURCE = "DAO_JOB_SOURCE"
ENV_JOB_CONSOLE_LOG = "DAO_JOB_CONSOLE_LOG"
ENV_QUEUE_DIR = "DAO_TASK_QUEUE_DIR"

EXIT_SUPERSEDED = 3
EXIT_CANCELLED = 4

# longest a task may run, counted from the start of its run; a task that is
# still running after this gets SIGALRM, which ends the process
TASK_TIMEOUT = 30 * 60
POLL_INTERVAL = 0.5
HISTORY_SIZE = 50

QUEUED = "queued"
RUNNING = "running"
JOINED = "joined"
DONE = "done"
ERROR = "error"
SUPERSEDED = "superseded"
CANCELLED = "cancelled"
TIMEOUT = "timeout"

ACTIVE_STATES = (QUEUED, RUNNING, JOINED)
FINISHED_STATES = (DONE, ERROR, SUPERSEDED, CANCELLED, TIMEOUT)

EXIT_CODES = {
    DONE: 0,
    ERROR: 1,
    TIMEOUT: 1,
    SUPERSEDED: EXIT_SUPERSEDED,
    CANCELLED: EXIT_CANCELLED,
}

RESTART = "restart"
JOIN = "join"

_current_job_id: str | None = None
_start_note: str | None = None
_stop_requested = False


def queue_dir() -> Path:
    configured = os.environ.get(ENV_QUEUE_DIR)
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "data"


def _queue_file() -> Path:
    return queue_dir() / "task_queue.json"


def _lock_file() -> Path:
    return queue_dir() / "task_queue.lock"


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def job_env(job_id: str, source: str, console_log: str | None = None) -> dict:
    """The environment for a child process that should enrol as ``job_id``."""
    env = dict(os.environ)
    env[ENV_JOB_ID] = job_id
    env[ENV_JOB_SOURCE] = source
    if console_log:
        env[ENV_JOB_CONSOLE_LOG] = str(console_log)
    else:
        env.pop(ENV_JOB_CONSOLE_LOG, None)
    return env


def job_key(task: str, args: list[str], task_def: dict | None = None) -> str:
    """Requests with the same key count as the same task.

    The key is the task's ``queue_group`` (the task name when it has none)
    followed by its extra arguments; ``--debug`` does not count.
    """
    group = (task_def or {}).get("queue_group", task)
    extra = [a for a in args if a != "--debug"]
    return " ".join([group, *extra])


def duplicate_policy(task_def: dict | None) -> str:
    return (task_def or {}).get("on_duplicate", JOIN)


def _proc_start_time(pid: int) -> str | None:
    """Start time of a live process, None when it is gone or a zombie.

    On Linux the start time from /proc tells a reused PID apart from the
    process that enrolled. Elsewhere only the PID itself is checked.
    """
    if Path("/proc/self/stat").exists():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        # the command name may hold spaces and brackets: split after the last ')'
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] in ("Z", "X"):
            return None
        return fields[19]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return "?"


def _alive(entry: dict) -> bool:
    start = _proc_start_time(entry["pid"])
    if start is None:
        return False
    recorded = entry.get("pid_start")
    return recorded in (None, "?") or start == "?" or start == recorded


HA_DATA_DIR = "/config/dao_data"


def _ensure_dir(directory: Path):
    """Create the queue directory.

    As a Home Assistant add-on the data directory is a link to
    /config/dao_data, which DaBase.make_data_path() creates when it is
    missing. The queue is opened before that, so it makes the same link
    instead of a plain directory that would stand in its way.
    """
    if (
        not os.path.lexists(directory)
        and ENV_QUEUE_DIR not in os.environ
        and os.path.isdir(HA_DATA_DIR)
    ):
        with contextlib.suppress(FileExistsError):
            os.symlink(HA_DATA_DIR, directory)
    directory.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def _locked():
    """The queue state, read and written back under an exclusive lock."""
    directory = queue_dir()
    _ensure_dir(directory)
    with open(_lock_file(), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(_queue_file().read_text(encoding="utf-8"))
                if not isinstance(state.get("jobs"), list):
                    raise ValueError("no job list")
            except (OSError, ValueError):
                state = {"jobs": []}
            _settle_dead(state["jobs"], time.time())
            _trim_history(state)
            yield state
            _trim_history(state)
            tmp = _queue_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            os.replace(tmp, _queue_file())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _finish_entry(entry: dict, state: str, now: float, exit_code: int | None = None):
    entry["state"] = state
    entry["finished"] = now
    entry["exit_code"] = EXIT_CODES[state] if exit_code is None else exit_code


def _settle_dead(jobs: list[dict], now: float):
    """Give every active entry whose process has gone its final state."""
    for entry in jobs:
        if entry["state"] not in ACTIVE_STATES or _alive(entry):
            continue
        if entry["state"] == RUNNING:
            if entry.get("stop_reason"):
                final = entry["stop_reason"]
            elif entry.get("deadline") and now >= entry["deadline"]:
                final = TIMEOUT
            else:
                final = ERROR
        else:
            # the process ended before its turn came
            final = ERROR
        entry["note"] = entry.get("note") or "process ended without reporting"
        _finish_entry(entry, final, now)


def _trim_history(state: dict):
    jobs = state["jobs"]
    # a joined entry still needs the result of the entry it waits for
    awaited = {j.get("joined_to") for j in jobs if j["state"] == JOINED}
    finished = [j for j in jobs if j["state"] in FINISHED_STATES and j["id"] not in awaited]
    if len(finished) <= HISTORY_SIZE:
        return
    finished.sort(key=lambda j: j.get("finished") or 0)
    drop = {j["id"] for j in finished[: len(finished) - HISTORY_SIZE]}
    state["jobs"] = [j for j in jobs if j["id"] not in drop]


def _find(jobs: list[dict], job_id: str) -> dict | None:
    for entry in jobs:
        if entry["id"] == job_id:
            return entry
    return None


def _queued(jobs: list[dict]) -> list[dict]:
    return sorted((j for j in jobs if j["state"] == QUEUED), key=lambda j: j["seq"])


def _running(jobs: list[dict]) -> dict | None:
    for entry in jobs:
        if entry["state"] == RUNNING:
            return entry
    return None


def _signal(pid: int, signum: int):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signum)


def enrol(
    task: str,
    args: list[str],
    task_def: dict | None = None,
    job_id: str | None = None,
    source: str = "cli",
    pid: int | None = None,
    console_log: str | None = None,
) -> dict:
    """Add this process to the queue and return its entry."""
    now = time.time()
    pid = os.getpid() if pid is None else pid
    key = job_key(task, args, task_def)
    entry = {
        "id": job_id or new_job_id(),
        "key": key,
        "task": task,
        "args": list(args),
        "source": source,
        "pid": pid,
        "pid_start": _proc_start_time(pid),
        "enrolled": now,
        "started": None,
        "finished": None,
        "deadline": None,
        "exit_code": None,
        "logfile": None,
        "console_log": console_log,
    }
    with _locked() as state:
        jobs = state["jobs"]
        existing = _find(jobs, entry["id"])
        if existing is not None:
            # the process that started this one has already enrolled it
            return dict(existing)
        seqs = [j["seq"] for j in jobs if j["state"] == QUEUED]
        same_running = next(
            (j for j in jobs if j["key"] == key and j["state"] == RUNNING), None
        )
        same_queued = next(
            (j for j in _queued(jobs) if j["key"] == key), None
        )
        if duplicate_policy(task_def) == RESTART:
            entry["state"] = QUEUED
            if same_running is not None:
                entry["seq"] = min(seqs, default=0) - 1
                if not same_running.get("stop_reason"):
                    same_running["stop_reason"] = SUPERSEDED
                    same_running["superseded_by"] = entry["id"]
                    _signal(same_running["pid"], signal.SIGTERM)
            elif same_queued is not None:
                entry["seq"] = same_queued["seq"]
            else:
                entry["seq"] = max(seqs, default=0) + 1
            if same_queued is not None:
                same_queued["superseded_by"] = entry["id"]
                _finish_entry(same_queued, SUPERSEDED, now)
        else:
            target = same_running if same_running is not None and not same_running.get(
                "stop_reason") else same_queued
            if target is not None:
                entry["state"] = JOINED
                entry["joined_to"] = target["id"]
                entry["seq"] = None
            else:
                entry["state"] = QUEUED
                entry["seq"] = max(seqs, default=0) + 1
        jobs.append(entry)
    return dict(entry)


def _say(message: str):
    print(message, flush=True)


def wait_turn(job_id: str, poll: float = POLL_INTERVAL) -> dict:
    """Block until the entry may run, or until it has a final state.

    Returns the entry. Its state is ``running`` when the caller should run
    the task now; any other state is final and the caller must not run.
    """
    last_report = None
    while True:
        with _locked() as state:
            jobs = state["jobs"]
            entry = _find(jobs, job_id)
            if entry is None:
                return {"id": job_id, "state": CANCELLED, "exit_code": EXIT_CANCELLED}
            if entry["state"] in FINISHED_STATES:
                return dict(entry)
            if entry["state"] == JOINED:
                target = _find(jobs, entry["joined_to"])
                if target is None or target["state"] in FINISHED_STATES:
                    final = target["state"] if target is not None else ERROR
                    _finish_entry(entry, final, time.time(),
                                  target.get("exit_code") if target else None)
                    if target is not None:
                        entry["logfile"] = target.get("logfile")
                    result = dict(entry)
                    result["target"] = dict(target) if target else None
                    return result
                report = ("joined", target["id"])
                message = (f"Taak {entry['task']} draait al of staat al in de wachtrij; "
                           f"wacht op het resultaat daarvan ({target['id']})")
            else:
                running = _running(jobs)
                queued = _queued(jobs)
                if running is None and queued and queued[0]["id"] == job_id:
                    now = time.time()
                    entry["state"] = RUNNING
                    entry["started"] = now
                    entry["deadline"] = now + TASK_TIMEOUT
                    result = dict(entry)
                    result["replaced"] = [
                        j["id"] for j in jobs if j.get("superseded_by") == job_id
                    ]
                    return result
                position = next(i for i, j in enumerate(queued) if j["id"] == job_id) + 1
                busy = running["task"] if running else queued[0]["task"]
                report = ("queued", position, busy)
                message = (f"Taak {entry['task']} staat in de wachtrij op plaats "
                           f"{position}, wacht op {busy}")
        if report != last_report:
            _say(message)
            last_report = report
        time.sleep(poll)


def finish(job_id: str, exit_code: int):
    with _locked() as state:
        entry = _find(state["jobs"], job_id)
        if entry is None or entry["state"] in FINISHED_STATES:
            return
        _finish_entry(entry, DONE if exit_code == 0 else ERROR, time.time(), exit_code)


def record_logfile(path: str):
    """Store the log file of the task this process is running."""
    if _current_job_id is None:
        return
    with _locked() as state:
        entry = _find(state["jobs"], _current_job_id)
        if entry is not None:
            entry["logfile"] = os.path.abspath(path)


def cancel(job_id: str) -> bool:
    """Cancel a queued, joined or running entry; False when it was not active.

    A running task is stopped with SIGTERM. One that has started writing its
    results finishes those first (see ``protect_output``).
    """
    with _locked() as state:
        entry = _find(state["jobs"], job_id)
        if entry is None or entry["state"] not in ACTIVE_STATES:
            return False
        if entry["state"] == RUNNING:
            if not entry.get("stop_reason"):
                entry["stop_reason"] = CANCELLED
                _signal(entry["pid"], signal.SIGTERM)
        else:
            _finish_entry(entry, CANCELLED, time.time())
        return True


def jobs() -> list[dict]:
    """All entries, active ones first in run order, then finished, newest first."""
    with _locked() as state:
        entries = [dict(j) for j in state["jobs"]]
    running = [j for j in entries if j["state"] == RUNNING]
    queued = sorted((j for j in entries if j["state"] == QUEUED), key=lambda j: j["seq"])
    for position, entry in enumerate(queued, start=1):
        entry["position"] = position
    joined = [j for j in entries if j["state"] == JOINED]
    finished = sorted(
        (j for j in entries if j["state"] in FINISHED_STATES),
        key=lambda j: j.get("finished") or 0,
        reverse=True,
    )
    return running + queued + joined + finished


def get(job_id: str) -> dict | None:
    for entry in jobs():
        if entry["id"] == job_id:
            return entry
    return None


def wait_enrolled(job_id: str, proc: subprocess.Popen, timeout: float = 30) -> dict | None:
    """Wait until the child process ``proc`` has enrolled as ``job_id``."""
    end = time.time() + timeout
    while time.time() < end:
        entry = get(job_id)
        if entry is not None or proc.poll() is not None:
            return entry
        time.sleep(0.1)
    return get(job_id)


def task_from_cmd(cmd: list[str]) -> tuple[str, list[str], dict] | None:
    """The task, its extra arguments and its definition in a day_ahead.py command."""
    from dao.prog.da_const import tasks

    for i, word in enumerate(cmd[:-1]):
        if Path(word).name == "day_ahead.py":
            name = "calc_debug" if cmd[i + 1] == "debug" else cmd[i + 1]
            for key, task_def in tasks.items():
                if name == key or name in task_def.get("aliases", []):
                    return key, list(cmd[i + 2:]), task_def
            return None
    return None


def spawn(cmd: list[str], source: str, cwd=None, console_log: str | None = None,
          **popen_kwargs) -> tuple[subprocess.Popen, str, dict | None]:
    """Start a task process and enrol it in the queue straight away.

    Enrolling here, not when the new process has loaded its modules, keeps
    the queue in the order in which the caller starts tasks. A thread reaps
    the process when it ends, unless the caller reads its output.
    """
    job_id = new_job_id()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=job_env(job_id, source, console_log), **popen_kwargs
    )
    if popen_kwargs.get("stdout") != subprocess.PIPE:
        threading.Thread(target=proc.wait, daemon=True).start()
    parsed = task_from_cmd(cmd)
    if parsed is None:
        return proc, job_id, wait_enrolled(job_id, proc)
    task, args, task_def = parsed
    enrol(task, args, task_def, job_id=job_id, source=source, pid=proc.pid,
          console_log=console_log)
    return proc, job_id, get(job_id)


def protect_output():
    """Let a stop request wait until this process ends.

    Called when a task starts writing its results: a SIGTERM that arrives
    after this point lets the task complete those writes instead of
    leaving them half done.
    """
    if _current_job_id is None or threading.current_thread() is not threading.main_thread():
        return

    def _defer(signum, frame):
        global _stop_requested
        if not _stop_requested:
            _stop_requested = True
            _say("Stopverzoek ontvangen; de resultaten worden eerst volledig weggeschreven")

    signal.signal(signal.SIGTERM, _defer)


def run_gate(task: str, args: list[str], task_def: dict | None = None) -> str:
    """Enrol this process and return its job id once it may run.

    When the entry ends without running (superseded, cancelled, or joined
    to another entry that has finished) the process exits here with the
    matching exit code.
    """
    global _current_job_id, _start_note
    entry = enrol(
        task,
        args,
        task_def,
        job_id=os.environ.get(ENV_JOB_ID) or None,
        source=os.environ.get(ENV_JOB_SOURCE, "cli"),
        console_log=os.environ.get(ENV_JOB_CONSOLE_LOG) or None,
    )
    result = wait_turn(entry["id"])
    if result["state"] == RUNNING:
        _current_job_id = entry["id"]
        notes = []
        waited = result["started"] - result["enrolled"]
        if waited >= 1:
            notes.append(f"{waited:.0f} s in de wachtrij gewacht")
        if result["replaced"]:
            notes.append("vervangt " + ", ".join(result["replaced"]))
        _start_note = f"Taak {entry['id']}: " + "; ".join(notes) if notes else None
        # SIGTERM keeps its default action until protect_output(): the task
        # stops at once, also while the solver is busy in native code
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(TASK_TIMEOUT)
        return entry["id"]
    target = result.get("target")
    if target is not None:
        _say(f"Resultaat van {target['task']} ({target['id']}): {target['state']}")
        logfile = target.get("logfile")
        if logfile and os.path.exists(logfile):
            with open(logfile, encoding="utf-8", errors="replace") as f:
                sys.stdout.write(f.read())
            sys.stdout.flush()
    elif result["state"] == SUPERSEDED:
        _say(f"Taak {task} vervangen door een nieuwere aanvraag "
             f"({result.get('superseded_by', '?')})")
    elif result["state"] == CANCELLED:
        _say(f"Taak {task} geannuleerd")
    sys.exit(result.get("exit_code") if result.get("exit_code") is not None
             else EXIT_CODES.get(result["state"], 1))


def current_job_id() -> str | None:
    return _current_job_id


def start_note() -> str | None:
    """How long this process waited for its turn and which entries it replaced."""
    return _start_note


def final_state(job_id: str) -> str | None:
    entry = get(job_id)
    return entry["state"] if entry else None
