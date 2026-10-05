"""
Chart of an optimisation run (calc_<yyyy-mm-dd>__<hh-mm>.png).

Drawing this chart takes several seconds (thousands of bar patches), more
than the solve itself for most setups, while it is only looked at now and
then. So the optimisation only saves the data behind the chart, as
calc_<stamp>.json next to where the png would go, and the png is drawn
from it the first time someone opens it in the web UI. With
graphics -> "generate png" set to true the png is drawn right away, as
before.

Everything here uses matplotlib's object API (Figure, not pyplot), so it
holds no global state and is safe to call from the webserver's threads.
"""

import datetime
import json
import logging
import math
import os
import tempfile

GRAPH_DATA_VERSION = 1
PNG_EXT = ".png"
DATA_EXT = ".json"
DATA_PATTERN = "calc_*" + DATA_EXT


def file_stem(start_dt: datetime.datetime) -> str:
    return "calc_" + start_dt.strftime("%Y-%m-%d__%H-%M")


def floats(values) -> list:
    return [float(v) for v in values]


def _iso(value):
    return None if value is None else value.isoformat()


def write_calc_graph(data: dict, images_dir: str, draw: bool = False) -> str:
    """
    Save the chart data of a run, and draw the png too when draw is True.
    :return: path of the json file
    """
    stem = os.path.join(images_dir, file_stem(datetime.datetime.fromisoformat(data["start_dt"])))
    data_path = stem + DATA_EXT
    _atomic_write(data_path, json.dumps(data).encode("utf-8"))
    if draw:
        draw_calc_graph(data, stem + PNG_EXT)
    return data_path


def ensure_png(images_dir: str, png_name: str) -> bool:
    """
    Draw png_name from its saved data if the png doesn't exist yet.
    :return: True if the png exists afterwards
    """
    png_path = os.path.join(images_dir, png_name)
    if os.path.exists(png_path):
        return True
    data_path = os.path.splitext(png_path)[0] + DATA_EXT
    if not os.path.exists(data_path):
        return False
    try:
        with open(data_path, "r") as f:
            data = json.load(f)
        draw_calc_graph(data, png_path)
    except Exception as ex:
        logging.error(f"Grafiek {png_name} kon niet worden gemaakt: {ex}")
        return False
    return True


def merge_pending(flist: list) -> list:
    """
    Turn a file list holding calc_*.png and calc_*.json entries
    ({"name": ..., "time": ...}) into a list of png names: each json whose
    png doesn't exist yet is listed under its png name, so the web UI can
    show it and draw it with ensure_png when it is opened. Order is kept.
    """
    pngs = {f["name"] for f in flist if f["name"].endswith(PNG_EXT)}
    result = []
    for f in flist:
        if f["name"].endswith(DATA_EXT):
            png_name = f["name"][: -len(DATA_EXT)] + PNG_EXT
            if png_name in pngs:
                continue
            f = dict(f, name=png_name)
        result.append(f)
    return result


def delete_graph(images_dir: str, png_name: str):
    """Remove a chart: the png and the data it is drawn from."""
    stem = os.path.splitext(os.path.join(images_dir, png_name))[0]
    for path in (stem + PNG_EXT, stem + DATA_EXT):
        if os.path.exists(path):
            os.remove(path)


def _atomic_write(path: str, content: bytes):
    # the webserver may read (or draw from) the file while it is written
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def draw_calc_graph(data: dict, png_path: str):
    import io
    import numpy as np
    import matplotlib.style
    import matplotlib.ticker as ticker
    import matplotlib.dates as mdates
    from matplotlib.figure import Figure

    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(max(logging.INFO, logging.getLogger().level))

    tijd = [datetime.datetime.fromisoformat(t) for t in data["tijd"]]
    U = len(tijd)
    start_dt = datetime.datetime.fromisoformat(data["start_dt"])
    start_prediction_dt = (
        datetime.datetime.fromisoformat(data["start_prediction_dt"])
        if data.get("start_prediction_dt")
        else None
    )
    horizon_extension = data["horizon_extension"]
    batteries = data["batteries"]
    B = len(batteries)
    show = data["show"]
    s = {key: np.array(values) for key, values in data["series"].items()}
    pv_p_org = s["pv_p_org"]
    pv_ac_p = s["pv_ac_p"]
    pv_p_opt = s["pv_p_opt"]
    org_l = s["org_l"]
    org_t = s["org_t"]
    c_l_p = s["c_l_p"]
    c_t_n = s["c_t_n"]
    base_n = s["base_n"]
    boiler_n = s["boiler_n"]
    heatpump_n = s["heatpump_n"]
    ev_n = s["ev_n"]
    mach_n = s["mach_n"]
    accu_in_n = s["accu_in_n"]
    accu_out_p = s["accu_out_p"]
    soc_t = list(data["soc_t"])
    pl = list(data["pl"])
    pt = list(data["pt"])
    p_spot = list(data["p_spot"])
    pl_avg = list(data["pl_avg"])

    with matplotlib.style.context(data["style"]):
        nrows = 3
        if show["battery_balance"] and B > 0:
            nrows += B
        fig = Figure(figsize=(8, 3 * nrows))
        axis = fig.subplots(nrows=nrows)

        # volgorde 1 pv_org 2 pv_ac 3 levering
        breedte = [
            (tijd[i + 1] - tijd[i]).total_seconds() * 0.9 / 86400
            for i in range(len(tijd) - 1)
        ]
        breedte.append(breedte[-1])
        if data["solar"]:
            axis[0].bar(
                tijd, pv_p_org, width=breedte, label="PV AC", color="green", align="edge"
            )
        # 2
        if sum(pv_ac_p) > 0:
            axis[0].bar(
                tijd,
                pv_ac_p,
                width=breedte,
                bottom=pv_p_org,
                label="PV DC",
                color="lime",
                align="edge",
            )
        # 3
        axis[0].bar(
            tijd,
            org_l,
            width=breedte,
            bottom=pv_p_org + pv_ac_p,
            label="Levering",
            color="#00bfff",
            align="edge",
        )

        axis[0].bar(
            tijd, base_n, width=breedte, label="Overig verbr.", color="#f1a603", align="edge"
        )
        if data["boiler"]:
            axis[0].bar(
                tijd,
                boiler_n,
                width=breedte,
                bottom=base_n,
                label="Boiler",
                color="#e39ff6",
                align="edge",
            )
        if data["heatpump"]:
            axis[0].bar(
                tijd,
                heatpump_n,
                width=breedte,
                bottom=base_n + boiler_n,
                label="WP",
                color="#a32cc4",
                align="edge",
            )
        if data["ev"]:
            axis[0].bar(
                tijd,
                ev_n,
                width=breedte,
                bottom=base_n + boiler_n + heatpump_n,
                label="EV laden",
                color="yellow",
                align="edge",
            )
        if data["machines"]:
            axis[0].bar(
                tijd,
                mach_n,
                width=breedte,
                bottom=base_n + boiler_n + heatpump_n + ev_n,
                label="Apparatuur",
                color="brown",
                align="edge",
            )
        axis[0].bar(
            tijd,
            org_t,
            width=breedte,
            bottom=base_n + boiler_n + heatpump_n + ev_n + mach_n,
            label="Teruglev.",
            color="#0080ff",
            align="edge",
        )
        axis[0].legend(loc="best", bbox_to_anchor=(1.05, 1.00))
        axis[0].set_ylabel("kW")
        ylim = math.ceil(data["max_y"])
        axis[0].set_ylim([-ylim, ylim])

        maanden = [
            "jan", "feb", "mrt", "apr", "mei", "jun",
            "jul", "aug", "sep", "okt", "nov", "dec"
        ]

        def nederlandse_datum(x, pos):
            datum = mdates.num2date(x)
            return f"{datum.day:02d} {maanden[datum.month - 1]}"

        axis[0].xaxis.set_major_locator(mdates.DayLocator())
        axis[0].xaxis.set_major_formatter(nederlandse_datum)

        if horizon_extension > 48:
            hours = [12]
        elif horizon_extension > 0:
            hours = [6, 12, 18]
        else:
            hours = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]

        axis[0].xaxis.set_minor_locator(mdates.HourLocator(byhour=hours))
        axis[0].xaxis.set_minor_formatter(mdates.DateFormatter("%H"))
        axis[0].tick_params(axis="x", which="minor", labelsize=10)
        axis[0].tick_params(axis="x", which="major", labelsize=12)

        axis[0].set_title(
            f"Berekend op: {start_dt.strftime('%d-%m-%Y %H:%M')}\nNiet geoptimaliseerd"
        )

        axis[1].bar(
            tijd, pv_p_opt, width=breedte, label="PV AC", color="green", align="edge"
        )
        axis[1].bar(
            tijd,
            accu_out_p,
            width=breedte,
            bottom=pv_p_opt,
            label="Accu uit",
            color="red",
            align="edge",
        )
        axis[1].bar(
            tijd,
            c_l_p,
            width=breedte,
            bottom=pv_p_opt + accu_out_p,
            label="Levering",
            color="#00bfff",
            align="edge",
        )

        axis[1].bar(
            tijd, base_n, width=breedte, label="Overig verbr.", color="#f1a603", align="edge"
        )
        if data["boiler"]:
            axis[1].bar(
                tijd,
                boiler_n,
                width=breedte,
                bottom=base_n,
                label="Boiler",
                color="#e39ff6",
                align="edge",
            )
        if data["heatpump"]:
            axis[1].bar(
                tijd,
                heatpump_n,
                width=breedte,
                bottom=base_n + boiler_n,
                label="WP",
                color="#a32cc4",
                align="edge",
            )
        if data["ev"]:
            axis[1].bar(
                tijd,
                ev_n,
                width=breedte,
                bottom=base_n + boiler_n + heatpump_n,
                label="EV laden",
                color="yellow",
                align="edge",
            )
        if data["machines"]:
            axis[1].bar(
                tijd,
                mach_n,
                width=breedte,
                bottom=base_n + boiler_n + heatpump_n + ev_n,
                label="Apparatuur",
                color="brown",
                align="edge",
            )
        if B > 0:
            axis[1].bar(
                tijd,
                accu_in_n,
                width=breedte,
                bottom=base_n + boiler_n + heatpump_n + ev_n + mach_n,
                label="Accu in",
                color="#ff8000",
                align="edge",
            )
        axis[1].bar(
            tijd,
            c_t_n,
            width=breedte,
            bottom=base_n + boiler_n + heatpump_n + ev_n + mach_n + accu_in_n,
            label="Teruglev.",
            color="#0080ff",
            align="edge",
        )
        axis[1].legend(loc="best", bbox_to_anchor=(1.05, 1.00))
        axis[1].set_ylabel("kW")
        axis[1].set_ylim([-ylim, ylim])
        axis[1].xaxis.set_major_locator(mdates.DayLocator())
        axis[1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        axis[1].xaxis.set_minor_locator(mdates.HourLocator(byhour=hours))
        axis[1].xaxis.set_minor_formatter(mdates.DateFormatter("%H"))
        axis[1].tick_params(axis="x", which="minor", labelsize=10)
        axis[1].tick_params(axis="x", which="major", labelsize=12)

        axis[1].set_title(
            f"Day Ahead geoptimaliseerd\nStrategie: {data['strategie']}"
            f" winst € {data['winst']:0.2f}"
        )
        axis[1].sharex(axis[0])

        # extra tijdstip voor sync aantal uur met laatste soc-waarde
        # eenmalig, de grafieken hieronder verwachten U+1 tijdstippen
        span = tijd[U - 1] - tijd[U - 2]
        tijd.append(tijd[U - 1] + span)
        breedte.append(breedte[-1])

        gr_no = 1
        if show["battery_balance"]:
            for battery in batteries:
                # make graph of battery
                gr_no += 1
                # extra uur voor sync aantal uur met laatste soc-waarde
                ac_p = np.array(battery["ac_p"] + [0])
                ac_n = np.array(battery["ac_n"] + [0])
                pv_p = np.array(battery["pv_p"] + [0])
                bat_p = np.array(battery["bat_p"] + [0])
                bat_n = np.array(battery["bat_n"] + [0])
                leg1 = axis[gr_no].bar(
                    tijd, ac_p, width=breedte, label="AC<->", color="red", align="edge"
                )
                leg2 = axis[gr_no].bar(
                    tijd,
                    bat_p,
                    label="BAT<->",
                    width=breedte,
                    bottom=ac_p,
                    color="blue",
                    align="edge",
                )
                if battery["pv_dc"]:
                    leg3 = axis[gr_no].bar(
                        tijd,
                        pv_p,
                        width=breedte,
                        label="PV->",
                        bottom=ac_p + bat_p,
                        color="lime",
                        align="edge",
                    )
                else:
                    leg3 = None
                axis[gr_no].bar(tijd, ac_n, width=breedte, color="red", align="edge")
                axis[gr_no].bar(
                    tijd,
                    bat_n,
                    width=breedte,
                    bottom=ac_n,
                    color="blue",
                    align="edge",
                )
                axis[gr_no].set_ylabel("kW")
                axis[gr_no].set_ylim([-ylim, ylim])

                axis[gr_no].xaxis.set_major_locator(mdates.DayLocator())
                axis[gr_no].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
                axis[gr_no].xaxis.set_minor_locator(mdates.HourLocator(byhour=hours))
                axis[gr_no].xaxis.set_minor_formatter(mdates.DateFormatter("%H"))
                axis[gr_no].tick_params(axis="x", which="minor", labelsize=10)
                axis[gr_no].tick_params(axis="x", which="major", labelsize=12)

                axis[gr_no].set_title(
                    f"Energiebalans per uur voor {battery['name']}"
                )
                axis[gr_no].sharex(axis[0])
                axis_20 = axis[gr_no].twinx()
                leg4 = axis_20.plot(
                    tijd, battery["soc"], label="% SoC", linestyle="solid", color="olive"
                )[0]
                axis_20.set_ylabel("% SoC")
                axis_20.set_ylim([0, 102])
                if battery["pv_dc"]:
                    labels = ["AC<->", "BAT<->", "PV->", "% SoC"]
                    handles = [leg1, leg2, leg3, leg4]
                else:
                    labels = ["AC<->", "BAT<->", "% SoC"]
                    handles = [leg1, leg2, leg4]
                axis[gr_no].legend(
                    handles=handles,
                    labels=labels,
                    loc="best",
                    bbox_to_anchor=(1.35, 1.00),
                )

        gr_no += 1
        ln1 = None
        line_styles = ["solid", "dashed", "dotted"]
        if B > 0:
            ln1 = axis[gr_no].plot(
                tijd, soc_t, label="SoC", linestyle=line_styles[0], color="olive"
            )
        axis[gr_no].xaxis.set_major_locator(mdates.DayLocator())
        axis[gr_no].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        axis[gr_no].xaxis.set_minor_locator(mdates.HourLocator(byhour=hours))
        axis[gr_no].xaxis.set_minor_formatter(mdates.DateFormatter("%H"))
        axis[gr_no].tick_params(axis="x", which="minor", labelsize=10)
        axis[gr_no].tick_params(axis="x", which="major", labelsize=12)

        axis[gr_no].set_ylabel("% SoC")
        axis[gr_no].set_xlabel("uren van de dag")

        axis[gr_no].set_ylim([0, 102])
        axis[gr_no].set_title("Verloop SoC en tarieven")
        axis[gr_no].sharex(axis[0])

        axis22 = axis[gr_no].twinx()
        if show["prices_consumption"]:
            pl.append(pl[-1])
            ln2 = axis22.step(
                tijd,
                np.array(pl),
                label="Tarief\nlevering",
                color="#00bfff",
                where="post",
            )
        else:
            ln2 = None

        if show["prices_production"]:
            pt.append(pt[-1])
            ln3 = axis22.step(
                tijd,
                np.array(pt),
                label="Tarief\nteruglev.",
                color="green",  # "#0080ff",
                where="post",
            )
        else:
            ln3 = None

        if show["prices_spot"]:
            p_spot.append(p_spot[-1])
            if horizon_extension > 0:
                tijd_fixed = [value for value in tijd if value <= start_prediction_dt]
                p_spot_fixed = p_spot[: len(tijd_fixed)]
                ln5 = axis22.step(
                    tijd_fixed,
                    np.array(p_spot_fixed),
                    label="Spot prices",
                    color="orange",
                    where="post",
                )
                tijd_pred = [value for value in tijd if value >= start_prediction_dt]
                p_spot_pred = p_spot[-len(tijd_pred):]
                ln6 = axis22.step(
                    tijd_pred,
                    np.array(p_spot_pred),
                    label="Pred.spot",
                    color="orange",
                    where="post",
                    linestyle="dashed",
                )
            else:
                ln5 = axis22.step(
                    tijd,
                    np.array(p_spot),
                    label="Spot prijzen",
                    color="orange",
                    where="post",
                )
                ln6 = None
        else:
            ln5 = None
            ln6 = None

        if show["average_consumption"]:
            pl_avg.append(pl_avg[-1])
            ln4 = axis22.plot(
                tijd,
                np.array(pl_avg),
                label="Tarief lev.\ngemid.",
                linestyle="dashed",
                color="#00bfff",
            )
        else:
            ln4 = None
        axis22.set_ylabel("euro/kWh")
        axis22.yaxis.set_major_formatter(ticker.FormatStrFormatter("% 1.2f"))
        bottom, top = axis22.get_ylim()
        if bottom > 0:
            axis22.set_ylim([0, top])
        lns = []
        for ln in (ln1, ln2, ln3, ln4, ln5, ln6):
            if ln:
                lns += ln
        labels = [line.get_label() for line in lns]
        axis22.legend(lns, labels, loc="best", bbox_to_anchor=(1.40, 1.00))

        fig.subplots_adjust(right=0.75)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
    _atomic_write(png_path, buf.getvalue())
