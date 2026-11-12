"""Exercise the reported route vulnerabilities through Flask requests."""
import importlib
import json
import logging
from copy import deepcopy
from subprocess import PIPE
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from hassapi.exceptions import ClientError as HassClientError
from sqlalchemy.exc import OperationalError

from dao.prog import da_task_queue

# V1 reads configuration and opens a log at import time. Keep those startup
# effects isolated from the route tests and the user's actual data.
with patch("dao.prog.config.loader.ConfigurationLoader.load_and_validate", return_value=None), \
        patch("logging.handlers.TimedRotatingFileHandler", return_value=logging.NullHandler()):
    from dao.webserver.app import app

api_routes = importlib.import_module("dao.webserver.app.api.routes")
v2_routes = importlib.import_module("dao.webserver.app.v2.routes")
v1_routes = importlib.import_module("dao.webserver.app.v1.routes")


@pytest.mark.parametrize("solar_present", [False, True])
def test_solar_menu_can_be_removed_repeatedly_and_restored(monkeypatch, solar_present):
    keys = ("home", "run", "reports", "savings", "solar", "settings")
    menu = {key: {"name": key} for key in keys if solar_present or key != "solar"}
    other_items = {key: value for key, value in menu.items() if key != "solar"}
    monkeypatch.setattr(v1_routes, "web_menu", menu)
    monkeypatch.setattr(v1_routes, "solar_web_menu", deepcopy(v1_routes.solar_web_menu))
    solar = SimpleNamespace(name="roof", ml_prediction=False)
    monkeypatch.setattr(v1_routes, "config", SimpleNamespace(solar=[solar], battery=[]))

    for _ in range(2):
        assert v1_routes.generate_solar_items() == {}
        assert v1_routes.web_menu == other_items

    solar.ml_prediction = True
    for _ in range(2):
        assert v1_routes.generate_solar_items() == {"roof": solar}
        assert tuple(v1_routes.web_menu) == keys
        assert {key: value for key, value in v1_routes.web_menu.items() if key != "solar"} == other_items


@pytest.fixture
def client():
    return app.test_client()


@pytest.mark.parametrize("path,subject", [("reports", "co2"), ("savings", "save_co2")])
def test_v2_co2_choices_and_forecast_period(client, monkeypatch, path, subject):
    monkeypatch.setattr(v2_routes, "co2_sensor_config", lambda: "sensor.co2")
    generate = Mock(return_value=["report"])
    monkeypatch.setattr(v2_routes, "reports_gen", generate)
    monkeypatch.setattr(v2_routes, "vite_tags", lambda _: "")
    response = client.get(f"/v2/{path}?subject={subject}&period=morgen")
    assert response.status_code == 200
    assert b'CO2' in response.data
    assert b'value="morgen"' not in response.data
    generate.assert_called_once_with(subject, "tabel", "vandaag")


@pytest.mark.parametrize("path,subject", [("reports", "co2"), ("savings", "save_co2")])
def test_v2_co2_unavailable_without_sensor(client, monkeypatch, path, subject):
    monkeypatch.setattr(v2_routes, "co2_sensor_config", lambda: None)
    assert client.get(f"/v2/{path}?subject={subject}").status_code == 404


def test_v2_warns_when_multiple_co2_sensors_are_configured(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "co2_sensor_config", lambda: ["sensor.one", "sensor.two"])
    monkeypatch.setattr(v2_routes, "reports_gen", Mock(return_value=["report"]))
    monkeypatch.setattr(v2_routes, "vite_tags", lambda _: "")
    response = client.get("/v2/reports?subject=co2")
    assert b"Multiple CO2 intensity sensors are summed" in response.data


@pytest.mark.parametrize("subject,method,options", [
    ("co2", "calc_co2_emission", "co2_graph_options"),
    ("save_co2", "calc_saving_co2", "saving_co2_graph_options"),
])
@pytest.mark.parametrize("view", ["tabel", "grafiek"])
def test_v2_co2_dispatch(monkeypatch, subject, method, options, view):
    report = Mock()
    report.periodes = {"vandaag": {"tot": v2_routes.datetime.datetime.now(), "interval": "uur"}}
    monkeypatch.setattr(v2_routes, "Report", Mock(return_value=report))
    v2_routes.reports_gen(subject, view, "vandaag")
    getattr(report, method).assert_called_once()
    if view == "grafiek":
        assert report.make_graph.call_args.args[2] is getattr(report, options)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("task", ["calc_optimum", "calc", "calc_zonder_debug"])
def test_api_task_and_aliases_execute_exactly_once(client, monkeypatch, tmp_path, method, task):
    program_dir = tmp_path / "prog"
    program_dir.mkdir()
    log_dir = tmp_path / "data" / "log"
    log_dir.mkdir(parents=True)
    monkeypatch.chdir(program_dir)
    monkeypatch.setitem(app.config, "DAO_WEB_PREVIEW", False)
    monkeypatch.setattr(api_routes, "PROG_DIR", program_dir)
    proc = Mock(returncode=0)
    proc.communicate.return_value = ("task output\n", "task warning\n")
    spawn = Mock(return_value=(proc, "job1", {"state": "running"}))
    monkeypatch.setattr(da_task_queue, "spawn", spawn)
    monkeypatch.setattr(da_task_queue, "final_state", Mock(return_value="done"))

    response = client.open(f"/api/run/{task}", method=method)

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert response.data == b"task output\ntask warning\n"
    spawn.assert_called_once_with(
        api_routes.tasks["calc_optimum"]["cmd"], source="api", cwd=program_dir,
        stdout=PIPE, stderr=PIPE, text=True,
    )
    logs = list(log_dir.glob("calc_*.log"))
    assert len(logs) == 1
    assert logs[0].read_text() == "task output\ntask warning\n"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unknown_task_is_escaped_plain_text(client, monkeypatch, method):
    runner = Mock(side_effect=AssertionError("Unknown task must not execute a command"))
    monkeypatch.setattr(da_task_queue, "spawn", runner)
    response = client.open("/api/run/%3Cimg%20src=x%20onerror=alert(1)%3E", method=method)
    assert response.status_code == 400
    assert response.mimetype == "text/plain"
    assert b"<img" not in response.data
    runner.assert_not_called()


@pytest.mark.parametrize("filename", [None, "../options.json", "/tmp/private.log", "..\\private.log", "file\x00.log"])
def test_log_rejects_invalid_paths(client, filename):
    response = client.get("/v2/view-file", query_string={} if filename is None else {"file": filename})
    assert response.status_code == 400


def test_log_rejects_symlink_escape(client, monkeypatch, tmp_path):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    secret = tmp_path / "secret.log"
    secret.write_text("private")
    (log_dir / "outside.log").symlink_to(secret)
    monkeypatch.setattr(v2_routes, "app_datapath", str(tmp_path))
    response = client.get("/v2/view-file", query_string={"file": "outside.log"})
    assert response.status_code == 400
    assert b"private" not in response.data


def test_log_valid_and_missing_files(client, monkeypatch, tmp_path):
    (tmp_path / "log").mkdir()
    (tmp_path / "log" / "task.log").write_text("normal log")
    monkeypatch.setattr(v2_routes, "app_datapath", str(tmp_path))
    monkeypatch.setattr(v2_routes, "render_template", lambda template, **kwargs: kwargs["content"])
    assert client.get("/v2/view-file?file=task.log").data == b"normal log"
    assert client.get("/v2/view-file?file=missing.log").status_code == 404


def test_data_errors_hide_exception_details(client, monkeypatch):
    report = Mock()
    report.get_data.side_effect = RuntimeError("secret database path and credentials")
    monkeypatch.setattr(api_routes, "Report", Mock(return_value=report))
    response = client.get("/api/data/?start=2026-01-14&end=2026-01-15&aggregate=hour")
    assert response.status_code == 500
    assert b"secret" not in response.data
    assert response.json == {"error": "Gegevens konden niet worden opgehaald"}


@pytest.mark.parametrize("query", ["", "start=bad&end=2026-01-15", "start=2026-01-14&end=2026-01-15&timezone=invalid"])
def test_bad_data_dates_are_client_errors(client, query):
    response = client.get("/api/data/?aggregate=hour&" + query)
    assert response.status_code == 400


@pytest.mark.parametrize("method,path,data", [
    ("POST", "/v2/task-exec", {"task": "clean_data"}),
    ("GET", "/v2/task-cancel", {}),
    ("GET", "/v2/task-state", {}),
    ("GET", "/v2/tasks", {}),
    ("GET", "/api/run/clean_data", {}),
    ("POST", "/api/run/clean_data", {}),
    ("POST", "/v1/run", {"current_bewerking": "clean_data"}),
    ("GET", "/v1/run", {"current_bewerking": "clean_data"}),
    ("POST", "/v1/", {"current_menu": "run", "current_bewerking": "clean_data"}),
    ("POST", "/v1/", {"current_menu": "run"}),
    ("POST", "/v1/", {"menu_run": "Run"}),
    ("GET", "/v1/status", {}),
    ("GET", "/v1/log", {}),
])
def test_preview_blocks_live_tasks_and_shared_state(client, monkeypatch, method, path, data):
    monkeypatch.setitem(app.config, "DAO_WEB_PREVIEW", True)
    live_calls = [
        (da_task_queue, "spawn"),
        (da_task_queue, "jobs"),
        (da_task_queue, "cancel"),
    ]
    mocks = []
    for module, name in live_calls:
        blocked = Mock(side_effect=AssertionError("Preview accessed live task execution/state"))
        monkeypatch.setattr(module, name, blocked)
        mocks.append(blocked)
    response = client.open(path, method=method, data=data)
    assert response.status_code == 403
    assert response.mimetype == "text/plain"
    assert b"synthetic scenario" in response.data
    for blocked in mocks:
        blocked.assert_not_called()


@pytest.mark.parametrize(("error", "status"), [
    (requests.ConnectionError("no route to host"), 503),
    (OperationalError("SELECT 1", {}, Exception("lost connection")), 503),
    (HassClientError("Home Assistant API is not running."), 503),
    (KeyError("secret database path"), 500),
])
def test_report_errors_are_json(client, monkeypatch, error, status):
    monkeypatch.setattr(api_routes, "Report", Mock(side_effect=error))
    response = client.get("/api/report/consumption/vandaag_en_morgen")
    assert response.status_code == status
    assert response.json["message"] == "Failed"
    assert b"secret" not in response.data


def test_report_returns_data(client, monkeypatch):
    report = Mock()
    report.get_api_data.return_value = '{ "message":"Success", "data": [] }'
    monkeypatch.setattr(api_routes, "Report", Mock(return_value=report))
    response = client.get("/api/report/consumption/vandaag_en_morgen?cumulate=1")
    assert response.status_code == 200
    assert response.json == {"message": "Success", "data": []}
    report.get_api_data.assert_called_once_with(
        "consumption", "vandaag_en_morgen", cumulate=True
    )


def _run_api(client, monkeypatch, tmp_path, final_state, returncode=0, query=""):
    program_dir = tmp_path / "prog"
    program_dir.mkdir()
    (tmp_path / "data" / "log").mkdir(parents=True)
    monkeypatch.chdir(program_dir)
    monkeypatch.setitem(app.config, "DAO_WEB_PREVIEW", False)
    monkeypatch.setattr(api_routes, "PROG_DIR", program_dir)
    proc = Mock(returncode=returncode)
    proc.communicate.return_value = ("out", "")
    monkeypatch.setattr(da_task_queue, "spawn", Mock(return_value=(proc, "job1", None)))
    monkeypatch.setattr(da_task_queue, "final_state", Mock(return_value=final_state))
    return client.get("/api/run/calc" + query)


@pytest.mark.parametrize("final_state,returncode,status", [
    ("done", 0, 200),
    ("error", 1, 500),
    ("timeout", -14, 500),
    ("superseded", -15, 409),
    ("cancelled", 4, 409),
    (None, 0, 200),
    (None, 1, 500),
])
def test_api_status_follows_the_queue_result(client, monkeypatch, tmp_path, final_state,
                                             returncode, status):
    response = _run_api(client, monkeypatch, tmp_path, final_state, returncode)
    assert response.status_code == status


def test_api_without_wait_answers_with_the_queue_position(client, monkeypatch):
    monkeypatch.setitem(app.config, "DAO_WEB_PREVIEW", False)
    spawn = Mock(return_value=(Mock(), "abc123", {"state": "queued", "position": 2}))
    monkeypatch.setattr(da_task_queue, "spawn", spawn)
    response = client.post("/api/run/calc?wait=0")

    assert response.status_code == 202
    assert response.json == {"job": "abc123", "task": "calc_optimum", "state": "queued",
                             "position": 2}
    assert spawn.call_args.args[0] == api_routes.tasks["calc_optimum"]["cmd"]
    assert spawn.call_args.kwargs["source"] == "api"


def test_gui_queues_a_task_while_another_one_runs(client, monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "DAO_WEB_PREVIEW", False)
    monkeypatch.setenv(da_task_queue.ENV_QUEUE_DIR, str(tmp_path))
    running = da_task_queue.enrol("get_meteo_data", [], job_id="meteo1")
    assert da_task_queue.wait_turn(running["id"], poll=0.01)["state"] == "running"
    spawn = Mock(return_value=(Mock(), "calc1", {"state": "queued", "position": 1}))
    monkeypatch.setattr(da_task_queue, "spawn", spawn)

    response = client.post("/v2/task-exec", data={"task": "calc_optimum"})

    assert response.status_code == 302
    assert "job=calc1" in response.headers["Location"]
    assert spawn.call_args.args[0][1] == "../prog/day_ahead.py"
    assert spawn.call_args.kwargs["source"] == "gui"


def test_task_state_lists_the_queue_and_cancels_per_job(client, monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "DAO_WEB_PREVIEW", False)
    monkeypatch.setenv(da_task_queue.ENV_QUEUE_DIR, str(tmp_path))
    running = da_task_queue.enrol("get_meteo_data", [], job_id="meteo1")
    da_task_queue.wait_turn(running["id"], poll=0.01)
    da_task_queue.enrol("calc_optimum", [], job_id="calc1", source="gui")
    monkeypatch.setattr(v2_routes, "vite_tags", lambda entry: "")

    page = client.get("/v2/task-state?job=calc1").get_data(as_text=True)
    assert "Waiting in the queue, position 1" in page
    assert "Meteoprognoses ophalen" in page
    assert "/v2/task-cancel?job=calc1" in page

    client.get("/v2/task-cancel?job=calc1")
    assert da_task_queue.final_state("calc1") == "cancelled"


@pytest.mark.parametrize("state,status,msg", [
    ("queued", "running", "staat in de wachtrij (plaats 1)"),
    ("joined", "running", "wacht op dezelfde opdracht"),
    ("superseded", "superseded", "vervangen door een nieuwere aanvraag"),
])
def test_v1_status_reports_the_queue_state(client, monkeypatch, tmp_path, state, status, msg):
    monkeypatch.setenv(da_task_queue.ENV_QUEUE_DIR, str(tmp_path))
    job = {"id": "v1job", "key": "calc", "task": "calc_optimum", "pid": 1, "source": "gui-v1",
           "state": state, "seq": 1, "finished": 1.0 if state == "superseded" else None}
    monkeypatch.setattr(da_task_queue, "_alive", lambda entry: True)
    (tmp_path / "task_queue.json").write_text(json.dumps({"jobs": [job]}))

    response = client.get("/v1/status").json

    assert response["status"] == status
    assert msg in response["msg"]


def test_joined_request_shows_the_log_of_the_run_it_waits_for(client, monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "DAO_WEB_PREVIEW", False)
    monkeypatch.setenv(da_task_queue.ENV_QUEUE_DIR, str(tmp_path))
    monkeypatch.setattr(v2_routes, "vite_tags", lambda entry: "")
    log = tmp_path / "meteo.log"
    log.write_text("meteo log line")
    running = da_task_queue.enrol("get_meteo_data", [], job_id="meteo1")
    da_task_queue.wait_turn(running["id"], poll=0.01)
    monkeypatch.setattr(da_task_queue, "_current_job_id", "meteo1")
    da_task_queue.record_logfile(str(log))
    da_task_queue.enrol("get_meteo_data", [], job_id="meteo2", source="gui")

    page = client.get("/v2/task-state?job=meteo2").get_data(as_text=True)

    assert "joined" in page
    assert "meteo log line" in page
