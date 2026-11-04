"""Serve real task routes and built assets with an in-memory task backend."""

import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="dao-browser-") as directory:
        data = Path(directory)
        (data / "log").mkdir()
        (data / "images").mkdir()
        options = json.loads((ROOT / "dao/data/options_example.json").read_text())
        options["homeassistant"] = {"host": "127.0.0.1", "ip port": 8123}
        (data / "options.json").write_text(json.dumps(options))
        (data / "secrets.json").write_text("{}")
        os.chdir(ROOT / "dao/webserver")
        os.environ["DAO_WEB_DATA_DIR"] = str(data)
        os.environ.pop("VITE_DEV", None)
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "dao/prog"))
        app = importlib.import_module("dao.webserver.app").app
        routes = importlib.import_module("dao.webserver.app.v2.routes")
        constants = importlib.import_module("dao.prog.da_const")
        flask = importlib.import_module("flask")
        queue = importlib.import_module("dao.prog.da_task_queue")
        jobs = []
        polls = {}

        def list_jobs():
            for job in jobs:
                if job["state"] == queue.RUNNING:
                    polls[job["id"]] = polls.get(job["id"], 0) + 1
                    if job["task"] == "complete" and polls[job["id"]] >= 3:
                        job.update(state=queue.DONE, exit_code=0, finished=time.time())
            return [dict(job) for job in jobs]

        def spawn(cmd, source, **kwargs):
            job = {"id": f"job{len(jobs) + 1}", "task": cmd[-1], "source": source,
                   "state": queue.RUNNING, "started": time.time(), "finished": None,
                   "logfile": None}
            jobs.append(job)
            return None, job["id"], dict(job)

        def cancel(job_id):
            for job in jobs:
                if job["id"] == job_id and job["state"] in queue.ACTIVE_STATES:
                    job.update(state=queue.CANCELLED, finished=time.time())
                    return True
            return False

        def forbid_process(*args, **kwargs):
            raise AssertionError("Browser fixtures must never launch task processes")

        routes.da_task_queue = SimpleNamespace(
            spawn=spawn, jobs=list_jobs, cancel=cancel,
            RUNNING=queue.RUNNING, QUEUED=queue.QUEUED, JOINED=queue.JOINED,
            ACTIVE_STATES=queue.ACTIVE_STATES,
        )
        queue.spawn = forbid_process
        constants.tasks = {
            "complete": {"name": "Run synthetic task", "cmd": ["never", "never", "complete"],
                         "ui_enabled": True},
            "waiting": {"name": "Run cancellable task", "cmd": ["never", "never", "waiting"],
                        "ui_enabled": True},
        }
        # Only this isolated fixture enables task routes; all task IO above is
        # replaced before serving. Production preview protections stay intact.
        app.config["DAO_WEB_PREVIEW"] = False

        @app.before_request
        def restrict_fixture_routes():
            if flask.request.endpoint == "v2.tasks":
                jobs.clear()
            if flask.request.endpoint not in {
                "v2.tasks", "v2.task_exec", "v2.task_state", "v2.task_cancel", "static",
            }:
                flask.abort(404)
            if flask.request.endpoint == "static" and not flask.request.path.startswith("/static/build/"):
                flask.abort(404)

        app.run(host="127.0.0.1", port=8765, debug=False)


if __name__ == "__main__":
    main()
