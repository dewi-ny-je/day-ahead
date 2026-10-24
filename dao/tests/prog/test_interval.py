"""
Tests voor de kwartier-resolutie in de rapportage/api.

Deze tests raken de database niet: ze controleren de hulpfuncties die bepalen
met welke stapgrootte de legacy-api ("/api/report/<veld>/<periode>") zijn
gegevens teruggeeft als de optimalisering met interval "15min" draait.
"""

import datetime
import sys

import pandas as pd

sys.path.append("../../../dao/prog")
from dao.prog.da_report import INTRADAY_INTERVALS, Report, interval_delta


def test_interval_delta():
    assert interval_delta("kwartier") == datetime.timedelta(minutes=15)
    assert interval_delta("uur") == datetime.timedelta(hours=1)
    # dag en maand worden per uur opgehaald
    assert interval_delta("dag") == datetime.timedelta(hours=1)
    assert interval_delta("maand") == datetime.timedelta(hours=1)


def test_intraday_intervals():
    assert "uur" in INTRADAY_INTERVALS
    assert "kwartier" in INTRADAY_INTERVALS


def test_generate_df_kwartier():
    vanaf = datetime.datetime(2025, 4, 6)
    tot = vanaf + datetime.timedelta(days=1)
    result = Report.generate_df(vanaf, tot, "kwartier")
    assert len(result) == 96
    assert result["tijd"].iloc[1] - result["tijd"].iloc[0] == datetime.timedelta(
        minutes=15
    )
    assert result["tijd"].iloc[-1] == tot - datetime.timedelta(minutes=15)


def test_generate_df_uur_ongewijzigd():
    vanaf = datetime.datetime(2025, 4, 6)
    tot = vanaf + datetime.timedelta(days=1)
    result = Report.generate_df(vanaf, tot, "uur")
    assert len(result) == 24


def test_generate_df_dag_met_kwartierstappen():
    """Een dag-rapport dat per kwartier wordt opgehaald houdt dag-labels."""
    vanaf = datetime.datetime(2025, 4, 6)
    tot = vanaf + datetime.timedelta(days=2)
    result = Report.generate_df(vanaf, tot, "dag", "kwartier")
    assert len(result) == 192
    assert set(result["dag"]) == {"2025-04-06", "2025-04-07"}


def test_spread_over_quarters_verdeelt_hoeveelheid():
    tijden = [
        datetime.datetime(2025, 4, 6, 9),
        datetime.datetime(2025, 4, 6, 10),
    ]
    df = pd.DataFrame({"tijd": tijden, "tot": tijden, "cons": [2.0, 4.0]})
    df.index = pd.to_datetime(df["tijd"])
    result = Report.spread_over_quarters(df, ["cons"])
    assert len(result) == 8
    assert list(result["cons"]) == [0.5] * 4 + [1.0] * 4
    # het totaal blijft gelijk
    assert result["cons"].sum() == df["cons"].sum()
    assert result["tijd"].iloc[1] == datetime.datetime(2025, 4, 6, 9, 15)


def test_spread_over_quarters_neemt_tarief_over():
    tijden = [datetime.datetime(2025, 4, 6, 9), datetime.datetime(2025, 4, 6, 10)]
    df = pd.DataFrame({"time": tijden, "da_cons": [0.25, 0.30]})
    df.index = pd.to_datetime(df["time"])
    result = Report.spread_over_quarters(
        df, ["da_cons"], quantity=False, time_column="time"
    )
    assert list(result["da_cons"]) == [0.25] * 4 + [0.30] * 4


def test_spread_over_quarters_laat_kwartierdata_ongemoeid():
    tijden = [
        datetime.datetime(2025, 4, 6, 9) + datetime.timedelta(minutes=15 * i)
        for i in range(4)
    ]
    df = pd.DataFrame({"tijd": tijden, "cons": [0.5, 0.5, 0.5, 0.5]})
    df.index = pd.to_datetime(df["tijd"])
    result = Report.spread_over_quarters(df, ["cons"])
    assert len(result) == 4
    assert list(result["cons"]) == [0.5] * 4


def test_recalc_df_ha_behoudt_kwartieren():
    """Kwartierprognoses mogen niet tot uurwaarden worden samengevoegd."""
    tijden = [
        datetime.datetime(2025, 4, 6, 9) + datetime.timedelta(minutes=15 * i)
        for i in range(4)
    ]
    df = pd.DataFrame(
        {
            "tijd": tijden,
            "consumption": [0.5] * 4,
            "production": [0.2] * 4,
            "da_cons": [0.30] * 4,
            "da_prod": [0.10] * 4,
            "datasoort": ["expected"] * 4,
        }
    )
    per_kwartier = Report.recalc_df_ha(None, df, "kwartier")
    assert len(per_kwartier) == 4
    assert per_kwartier["consumption"].sum() == 2.0

    per_uur = Report.recalc_df_ha(None, df, "uur")
    assert len(per_uur) == 1
    assert per_uur["consumption"].iloc[0] == 2.0


def _kwartieren(start: datetime.datetime, aantal: int) -> list:
    return [start + datetime.timedelta(minutes=15 * i) for i in range(aantal)]


def test_to_api_intervals_voegt_kwartieren_samen_in_uurdeel():
    """
    In het deel met voorspelde prijzen rekent de optimalisering per uur:
    de uurwaarde staat op het eerste kwartier, de andere drie zijn 0.
    De api moet daar een uur-interval van maken.
    """
    tijden = _kwartieren(datetime.datetime(2025, 4, 6, 22), 12)
    waarden = [0.25] * 4 + [1.0, 0, 0, 0] + [2.0, 0, 0, 0]
    df = pd.DataFrame(
        {"time": tijden, "value": waarden, "datasoort": ["expected"] * 12}
    )
    hourly_range = (datetime.datetime(2025, 4, 6, 23), datetime.datetime(2025, 4, 7, 1))
    result = Report.to_api_intervals(df, ["value"], "kwartier", hourly_range)
    assert len(result) == 6
    assert list(result["interval_minutes"]) == [15] * 4 + [60] * 2
    assert list(result["value"]) == [0.25] * 4 + [1.0, 2.0]
    assert result["time"].iloc[4] == datetime.datetime(2025, 4, 6, 23)
    assert result["time"].iloc[5] == datetime.datetime(2025, 4, 7, 0)
    # het totaal blijft gelijk
    assert result["value"].sum() == df["value"].sum()


def test_to_api_intervals_zonder_uurdeel():
    tijden = _kwartieren(datetime.datetime(2025, 4, 6, 9), 4)
    df = pd.DataFrame(
        {"time": tijden, "value": [0.5] * 4, "datasoort": ["recorded"] * 4}
    )
    result = Report.to_api_intervals(df, ["value"], "kwartier")
    assert len(result) == 4
    assert list(result["interval_minutes"]) == [15] * 4

    per_uur = Report.to_api_intervals(df, ["value"], "uur")
    assert list(per_uur["interval_minutes"]) == [60] * 4


class _FakeDb:
    def __init__(self, moments):
        self.moments = moments

    def get_column_data(self, tablename, column_name, start=None, end=None):
        utc = [
            m.timestamp() for m in self.moments if start <= m and (end is None or m < end)
        ]
        return pd.DataFrame({"utc": utc})


def _report_met_prognoses(moments, interval="15min"):
    report = Report.__new__(Report)
    report.interval = interval
    report.db_da = _FakeDb(moments)
    return report


def test_get_hourly_start_vindt_uurdeel():
    now = datetime.datetime.now()
    begin = datetime.datetime(now.year, now.month, now.day, now.hour)
    kwartieren = _kwartieren(begin, 8)
    uren = [kwartieren[-1] + datetime.timedelta(minutes=15, hours=i) for i in range(5)]
    report = _report_met_prognoses(kwartieren + uren)
    tot = begin + datetime.timedelta(days=2)
    assert report.get_hourly_range(begin, tot) == (
        uren[0],
        uren[-1] + datetime.timedelta(hours=1),
    )


def test_get_hourly_start_zonder_uurdeel():
    now = datetime.datetime.now()
    begin = datetime.datetime(now.year, now.month, now.day, now.hour)
    report = _report_met_prognoses(_kwartieren(begin, 20))
    tot = begin + datetime.timedelta(days=2)
    assert report.get_hourly_range(begin, tot) is None
    # met een uur-interval is er nooit een apart uurdeel
    uren = [begin + datetime.timedelta(hours=i) for i in range(5)]
    assert _report_met_prognoses(uren, "1hour").get_hourly_range(begin, tot) is None


def _api_report(df_balance, hourly_range):
    import json

    report = Report.__new__(Report)
    report.interval = "15min"
    report.report_interval = "kwartier"
    report.time_zone = "Europe/Amsterdam"
    report.energy_balance_dict = {"base": {}}
    report.several_dict = {}
    vanaf = datetime.datetime(2025, 4, 6, 22)
    report.periodes = {
        "vandaag": {
            "vanaf": vanaf,
            "tot": vanaf + datetime.timedelta(hours=3),
            "interval": "uur",
        }
    }
    report.get_hourly_range = lambda _vanaf, _tot: hourly_range
    report.get_energy_balance_data = lambda *args, **kwargs: (df_balance, None)
    return report, json


def test_get_api_data_geeft_gemiddeld_vermogen():
    tijden = _kwartieren(datetime.datetime(2025, 4, 6, 22), 12)
    waarden = [0.25] * 4 + [1.0, 0, 0, 0] + [2.0, 0, 0, 0]
    df_balance = pd.DataFrame(
        {"tijd": tijden, "base": waarden, "datasoort": ["expected"] * 12}
    )
    report, json = _api_report(
        df_balance, (datetime.datetime(2025, 4, 6, 23), datetime.datetime(2025, 4, 7, 1))
    )

    data = json.loads(report.get_api_data("base", "vandaag"))["data"]
    assert [r["time"] for r in data] == [
        "2025-04-06 22:00",
        "2025-04-06 22:15",
        "2025-04-06 22:30",
        "2025-04-06 22:45",
        "2025-04-06 23:00",
        "2025-04-07 00:00",
    ]
    # 0.25 kWh per kwartier en 1 kWh per uur zijn allebei 1 kW
    assert [r["value"] for r in data] == [1.0] * 4 + [1.0, 2.0]
    assert [r["interval_minutes"] for r in data] == [15] * 4 + [60] * 2
    assert list(data[0].keys())[0] == "time_ts"

    # cumulatief blijft energie (kWh)
    cumul = json.loads(report.get_api_data("base", "vandaag", cumulate=True))["data"]
    assert [r["value"] for r in cumul] == [0.25, 0.5, 0.75, 1.0, 2.0, 4.0]


def test_to_api_intervals_laat_staart_na_uurdeel_per_kwartier():
    """Na het uur-deel (bijv. de verlengde pv-prognose) blijven kwartieren staan."""
    tijden = _kwartieren(datetime.datetime(2025, 4, 6, 23), 8)
    waarden = [1.0, 0, 0, 0] + [0.25] * 4
    df = pd.DataFrame(
        {"time": tijden, "value": waarden, "datasoort": ["expected"] * 8}
    )
    hourly_range = (datetime.datetime(2025, 4, 6, 23), datetime.datetime(2025, 4, 7, 0))
    result = Report.to_api_intervals(df, ["value"], "kwartier", hourly_range)
    assert list(result["interval_minutes"]) == [60] + [15] * 4
    assert list(result["value"]) == [1.0] + [0.25] * 4


def test_to_api_intervals_houdt_dubbel_uur_einde_zomertijd_op_volgorde():
    """
    Op de dag dat de zomertijd eindigt komen de kwartieren van 02:00 tot 03:00
    twee keer voor. Na het sorteren moet van elke dubbele tijd die van de
    zomertijd voorop blijven, zodat localize_local_times ze goed toewijst.
    """
    from dao.prog.utils import localize_local_times

    eerste = _kwartieren(datetime.datetime(2026, 10, 25, 1, 45), 5)
    tweede = _kwartieren(datetime.datetime(2026, 10, 25, 2), 5)
    tijden = eerste + tweede
    df = pd.DataFrame(
        {"time": tijden, "value": list(range(10)), "datasoort": ["recorded"] * 10}
    )
    result = Report.to_api_intervals(df, ["value"], "kwartier")
    ts = localize_local_times(result["time"], "Europe/Amsterdam")
    assert ts.is_unique
    # op het werkelijke tijdstip gesorteerd staan de waarden weer in hun volgorde
    assert list(result.loc[ts.sort_values().index, "value"]) == list(range(10))
