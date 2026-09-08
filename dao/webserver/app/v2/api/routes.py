import json
import math
import os
import threading
import time

from flask import Blueprint, jsonify, render_template, request, redirect, url_for
from markupsafe import escape
from dao.prog.da_report import Report
from subprocess import run as subprocess_run
from dao.prog.da_base import DaBase
from dao.prog.config.loader import ConfigurationLoader
from dao.prog.version import __version__
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from ..routes import (
    app_datapath,
    get_solar_items_with_ml,
    get_task_state,
    run_and_log,
    save_task_state,
)

api = Blueprint("api", __name__)

DEFAULT_TIMEZONE = "Europe/Amsterdam"
BASELOAD_PATH = "../data/baseload/"
MODEL_PATH = "../data/prediction/models/"
STATS_SUFFIX = "_stats.json"

# De taken die via /task-exec/ gestart mogen worden, met de argumenten voor
# day_ahead.py. De aliassen komen overeen met de namen die de web-ui gebruikt.
API_TASKS = {
    "optimize": ["calc"],
    "optimize_regular": ["calc"],
    "optimize_debug": ["debug", "calc"],
    "prices": ["prices"],
    "update_prices": ["prices"],
    "meteo": ["meteo"],
    "update_meteo": ["meteo"],
    "tibber": ["tibber"],
    "update_tibber": ["tibber"],
    "calc_baseloads": ["calc_baseloads"],
    "train_ml": ["train"],
    "consolidate": ["consolidate"],
    "clean": ["clean"],
}


@api.route("/data/")
def data():
    """
    Retourneert in json de data
    :return: de gevraagde data in json formaat
    """
    data_report = Report()
    start = request.args.get('start')
    end = request.args.get('end')
    aggregate = request.args.get('aggregate')
    fields = request.args.get('fields')

    if fields:
        fields = fields.split(",")

    timezone_raw = request.args.get('timezone') if None else "Europe/Amsterdam"

    try:
        data = data_report.get_data(
            start=datetime.fromisoformat(start).replace(tzinfo=ZoneInfo(timezone_raw)),
            end=datetime.fromisoformat(end).replace(tzinfo=ZoneInfo(timezone_raw)),
            aggregate=aggregate,
            var_codes=fields,
        )

    except Exception as e:
        return {"error": str(e)}, 500

    def format_ts(dt, aggregate: str) -> str:
        if aggregate == "15min":
            return dt.strftime("%Y-%m-%d %H:%M")
        elif aggregate == "hour":
            return dt.strftime("%Y-%m-%d %H:00")
        else:
            return dt.strftime("%Y-%m-%d")

    data = [
        {**row, "ts": format_ts(row["ts"], aggregate)}
        for row in data
    ]

    return data

@api.route("/run/<string:task>")
def run(task: str):
    tasks = DaBase.generate_tasks()
    if task in tasks.keys():
        proc = subprocess_run(tasks[task]["cmd"], capture_output=True, text=True)
        data = proc.stdout
        err = proc.stderr
        log_content = data + err

        return log_content, {"Content-Type": "text/plain"}
    else:
        return "Unknown task: " + escape(task)


@api.route("/data-sql-ha/")
def data_sql_ha():
    """
    Retourneert in json de data
    :return: de gevraagde data in json formaat
    """
    data_report = Report()
    start = request.args.get('start')
    end = request.args.get('end')
    aggregate = request.args.get('aggregate')
    fields = request.args.get('fields')

    if fields:
        fields = fields.split(",")

    timezone_raw = request.args.get("timezone") if None else "Europe/Amsterdam"

    query = data_report.get_ha_data_query(
            start=datetime.fromisoformat(start).replace(tzinfo=ZoneInfo(timezone_raw)),
            end=datetime.fromisoformat(end).replace(tzinfo=ZoneInfo(timezone_raw)),
            var_codes=fields,
            step=timedelta(days=1)
        )

    return str(query)


# ---------------------------------------------------------------------------
# JSON-api t.b.v. externe clients (o.a. het Home Assistant dashboard)
# ---------------------------------------------------------------------------
# Alle onderstaande endpoints leveren json met tijdstempels in ISO-8601 met
# tijdzone-offset, zodat een client (bijv. apexcharts-card in Home Assistant)
# ze zonder aannames over de lokale tijdzone kan verwerken.


class _BadRequest(Exception):
    """
    Een onbruikbare queryparameter. Levert een nette 400 in plaats van de
    algemene foutpagina van Flask, zodat een typefout van de client niet als
    serverfout oogt.
    """


@api.errorhandler(_BadRequest)
def _handle_bad_request(ex: _BadRequest):
    return _error(str(ex), 400)


def _timezone() -> ZoneInfo:
    """De tijdzone waarin de dao-data wordt vastgelegd."""
    name = request.args.get("timezone") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise _BadRequest(f"Unknown timezone: {escape(name)}")


def _local_iso(value: datetime) -> str:
    """
    Formatteert een datetime als ISO-8601 met offset in de gevraagde tijdzone.
    Een naive datetime komt van de server en wordt dus als servertijd gelezen
    en daarna omgerekend; hem alleen van een ander label voorzien zou een
    ander tijdstip opleveren.
    """
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(_timezone()).isoformat()


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        raise _BadRequest(f"Invalid date: {escape(value)}")


def _float_or_none(value) -> float | None:
    """Maakt van pandas/numpy-waarden een json-serialiseerbare float of None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def _error(message: str, status: int = 500):
    return jsonify({"error": message}), status


@api.route("/status/")
def status():
    """
    Overzicht van versie, actieve taak en de belangrijkste configuratie.
    Bedoeld als "is dao bereikbaar en wat doet het"-endpoint.
    """
    result = {
        "version": __version__,
        "time": _local_iso(datetime.now()),
        "timezone": str(_timezone()),
        "task": _task_state_payload(),
    }
    try:
        loader = ConfigurationLoader(Path(app_datapath + "options.json"))
        config = loader.load_and_validate()
        result["config"] = {
            "interval": getattr(config, "interval", None),
            "strategy": getattr(config, "strategy", None),
            "solar_count": len(config.solar),
            "battery_count": len(config.battery),
            "ml_solar_devices": sorted(get_solar_items_with_ml().keys()),
        }
    except Exception as ex:
        result["config"] = None
        result["config_error"] = str(ex)
    return jsonify(result)


@api.route("/vars/")
def vars_list():
    """De variabelen (codes) waarvoor daadwerkelijk data aanwezig is."""
    try:
        return jsonify(Report().get_vars())
    except Exception as ex:
        return _error(str(ex))


@api.route("/task-state/")
def task_state_json():
    """De status van de taak die via /task-exec/ is gestart."""
    return jsonify(_task_state_payload(include_log=True))


def _task_state_payload(include_log: bool = False) -> dict:
    state = get_task_state()
    started = state.get("started")
    last_update = (
        time.time() if state.get("status") == "running" else state.get("last_update")
    )
    payload = {
        "status": state.get("status", "idle"),
        "task": state.get("task"),
        "returncode": state.get("returncode"),
        "started": _local_iso(datetime.fromtimestamp(started)) if started else None,
        "seconds_running": (
            int(last_update - started)
            if started is not None and last_update is not None
            else None
        ),
        "logfile": os.path.basename(state["logfile"]) if state.get("logfile") else None,
    }
    if include_log:
        payload["log"] = _read_log(state.get("logfile"))
    return payload


def _read_log(logfile: str | None, max_chars: int = 8000) -> str:
    if not logfile:
        return ""
    try:
        with open(logfile, "r") as f:
            content = f.read()
    except OSError:
        return ""
    return content[-max_chars:]


@api.route("/task-exec/", methods=["POST"])
def task_exec_json():
    """
    Start een taak asynchroon; de voortgang is op te vragen via /task-state/.
    De taak mag zowel als json ({"task": ...}) als form-encoded worden
    meegegeven.
    """
    payload = request.get_json(silent=True)
    if payload is not None and not isinstance(payload, dict):
        return _error('JSON body must be an object, e.g. {"task": "optimize"}', 400)
    if not payload:
        payload = request.form.to_dict()
    task = payload.get("task")
    if task not in API_TASKS:
        return _error(
            f"Unknown task: {escape(str(task))}. "
            f"Valid tasks: {', '.join(sorted(API_TASKS))}",
            400,
        )

    current_state = get_task_state()
    if current_state.get("status") == "running":
        return _error(f"Task already running: {current_state.get('task')}", 409)

    state = {
        "status": "running",
        "started": time.time(),
        "task": task,
        "returncode": None,
        "logfile": None,
    }
    save_task_state(state)

    cmd = ["python3", "../prog/day_ahead.py", *API_TASKS[task]]
    threading.Thread(target=run_and_log, args=(cmd, state), daemon=True).start()

    return jsonify({"started": True, "task": task}), 202


@api.route("/task-cancel/", methods=["POST"])
def task_cancel_json():
    """Breekt de lopende taak af."""
    state = get_task_state()
    if state.get("status") != "running":
        return jsonify({"cancelled": False, "status": state.get("status", "idle")})
    state["status"] = "cancelled"
    save_task_state(state)
    return jsonify({"cancelled": True, "task": state.get("task")})


@api.route("/prices/")
def prices():
    """
    De day ahead tarieven per uur, inclusief morgen zodra die gepubliceerd zijn,
    met de belangrijkste statistieken per dag.
    """
    tz = _timezone()
    today = datetime.now(tz).date()
    start_date = _parse_date(request.args.get("start"), today)
    end_date = _parse_date(request.args.get("end"), today + timedelta(days=2))
    start = datetime.combine(start_date, dtime.min, tzinfo=tz)
    end = datetime.combine(end_date, dtime.min, tzinfo=tz)

    try:
        rows = Report().get_data(
            start=start, end=end, aggregate="hour", var_codes=["da"]
        )
    except Exception as ex:
        return _error(str(ex))

    # get_data levert per tijdstip een dict {"ts": .., "<code>": {"v": .., "f": ..}}
    series = []
    for row in rows:
        prices_row = row.get("da") or {}
        value = _float_or_none(prices_row.get("v"))
        if value is None:
            value = _float_or_none(prices_row.get("f"))
        if value is None:
            continue
        series.append({"time": _local_iso(row["ts"]), "price": value})

    return jsonify(
        {
            "dim": "euro/kWh",
            "series": series,
            "today": _price_stats(series, today, tz),
            "tomorrow": _price_stats(series, today + timedelta(days=1), tz),
        }
    )


def _price_stats(series: list, day: date, tz: ZoneInfo) -> dict | None:
    """Min/max/gemiddelde en de goedkoopste/duurste uren van één dag."""
    day_rows = [
        row for row in series if datetime.fromisoformat(row["time"]).date() == day
    ]
    if not day_rows:
        return None
    prices_list = [row["price"] for row in day_rows]
    ordered = sorted(day_rows, key=lambda row: row["price"])
    return {
        "date": day.isoformat(),
        "hours": len(day_rows),
        "min": min(prices_list),
        "max": max(prices_list),
        "average": round(sum(prices_list) / len(prices_list), 5),
        "cheapest": ordered[0],
        "most_expensive": ordered[-1],
        "cheapest_hours": [row["time"] for row in ordered[:3]],
    }


@api.route("/solar-devices/")
def solar_devices():
    """De pv-installaties waarvoor een ml-voorspelling is geconfigureerd."""
    try:
        items = get_solar_items_with_ml()
    except Exception as ex:
        return _error(str(ex))

    result = []
    for name, option in items.items():
        model_file = _model_path(name)
        result.append(
            {
                "name": name,
                "capacity": _float_or_none(getattr(option, "total_capacity", None)),
                "tilt": _float_or_none(getattr(option, "effective_tilt", None)),
                "orientation": _float_or_none(
                    getattr(option, "effective_orientation", None)
                ),
                "ml_prediction": True,
                "model_trained": os.path.isfile(model_file),
            }
        )
    return jsonify(result)


def _model_name(name: str) -> str:
    """Dezelfde normalisatie als SolarPredictor gebruikt voor bestandsnamen."""
    return name.replace(" ", "_").replace("-", "_")


def _model_path(name: str) -> str:
    return MODEL_PATH + _model_name(name) + ".pkl"


@api.route("/solar-ml/")
def solar_ml():
    """
    Vergelijking per uur van de gemeten productie met de dao-voorspelling en de
    ml-voorspelling voor één pv-installatie en één dag.
    """
    from dao.prog.da_report import calc_r2

    try:
        items = get_solar_items_with_ml()
    except Exception as ex:
        return _error(str(ex))

    if not items:
        return _error("No solar device with ml_prediction configured", 404)

    device_name = request.args.get("device") or next(iter(items))
    if device_name not in items:
        return _error(f"Unknown solar device: {escape(device_name)}", 404)

    tz = _timezone()
    day = _parse_date(request.args.get("date"), datetime.now(tz).date())

    try:
        data = Report().calc_solar_data(items[device_name], day, "grafiek")
    except FileNotFoundError as ex:
        return _error(str(ex), 404)
    except Exception as ex:
        return _error(str(ex))

    rows = []
    for row in data.itertuples():
        moment = datetime.combine(day, dtime.min, tzinfo=tz) + timedelta(
            hours=int(str(row.uur)[0:2])
        )
        rows.append(
            {
                "time": moment.isoformat(),
                "radiation_measured": _float_or_none(row.gemeten_straling),
                "radiation_forecast": _float_or_none(row.prognose_straling),
                "production_measured": _float_or_none(row.gemeten_prod),
                "production_dao": _float_or_none(row.prognose_dao),
                "production_ml": _float_or_none(row.prognose_ml),
            }
        )

    def _sum(key: str) -> float:
        return round(sum(row[key] or 0 for row in rows), 3)

    return jsonify(
        {
            "device": device_name,
            "date": day.isoformat(),
            "rows": rows,
            "totals": {
                "production_measured": _sum("production_measured"),
                "production_dao": _sum("production_dao"),
                "production_ml": _sum("production_ml"),
            },
            "r2": {
                "radiation": _float_or_none(
                    calc_r2(data["gemeten_straling"], data["prognose_straling"])
                ),
                "production_dao": _float_or_none(
                    calc_r2(data["gemeten_prod"], data["prognose_dao"])
                ),
                "production_ml": _float_or_none(
                    calc_r2(data["gemeten_prod"], data["prognose_ml"])
                ),
            },
        }
    )


@api.route("/baseloads/")
def baseloads():
    """
    De berekende basislast per weekdag (0 = maandag) met 24 uurwaarden,
    zoals weggeschreven door de taak "calc_baseloads".
    """
    tz = _timezone()
    now = datetime.now(tz)
    weekdays = {}
    updated = None
    for weekday in range(7):
        file_name = BASELOAD_PATH + f"baseload_{weekday}.json"
        try:
            with open(file_name, "r") as f:
                weekdays[weekday] = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        mtime = os.path.getmtime(file_name)
        updated = mtime if updated is None else max(updated, mtime)

    if not weekdays:
        return _error(
            'No baseloads calculated yet, run the task "calc_baseloads" first', 404
        )

    today = weekdays.get(now.weekday(), [])
    midnight = datetime.combine(now.date(), dtime.min, tzinfo=tz)
    return jsonify(
        {
            "dim": "kWh",
            "updated": _local_iso(datetime.fromtimestamp(updated)) if updated else None,
            "weekdays": weekdays,
            "today": {
                "weekday": now.weekday(),
                "series": [
                    {
                        "time": (midnight + timedelta(hours=hour)).isoformat(),
                        "value": _float_or_none(value),
                    }
                    for hour, value in enumerate(today)
                ],
            },
        }
    )


@api.route("/ml-models/")
def ml_models():
    """
    De trainingsresultaten van de ml-modellen: wanneer getraind en hoe goed
    (r², mae, rmse) plus het belang van de gebruikte features.
    """
    try:
        items = get_solar_items_with_ml()
    except Exception as ex:
        return _error(str(ex))

    result = []
    for name in items:
        model_file = _model_path(name)
        stats_file = MODEL_PATH + _model_name(name) + STATS_SUFFIX
        entry = {
            "name": name,
            "model": _model_name(name),
            "trained": os.path.isfile(model_file),
            "trained_at": (
                _local_iso(datetime.fromtimestamp(os.path.getmtime(model_file)))
                if os.path.isfile(model_file)
                else None
            ),
            "stats": None,
        }
        try:
            with open(stats_file, "r") as f:
                entry["stats"] = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        result.append(entry)
    return jsonify(result)
