"""
Bonus op teruglevering binnen een dagelijks tijdvenster,
bijv. de Zonneplan Zonnebonus: 10% over (marktprijs + 0,02) van zonsopkomst tot zonsondergang.
"""

import bisect
import datetime
import hashlib
import json
import logging
import math
import os
import tempfile

import ephem

from dao.prog.config.models.pricing import ProductionBonusConfig

# KNMI De Bilt, referentie voor zonsopkomst/-ondergang in Nederland
NL_CENTER = (52.101, 5.177)


class ProductionBonus:
    def __init__(
        self, config: ProductionBonusConfig, latitude: float, longitude: float
    ):
        self.config = config
        if config.sun_location == "nl_center":
            latitude, longitude = NL_CENTER
        self.latitude = latitude
        self.longitude = longitude
        self._days = sorted(config.percentage.keys())
        self._windows: dict[datetime.date, tuple[float, float]] = {}

    def percentage(self, day: datetime.date) -> float:
        """Bonuspercentage op dag; 0 voor de eerste datum."""
        day_str = day.strftime("%Y-%m-%d")
        index = bisect.bisect_right(self._days, day_str)
        if index == 0:
            return 0.0
        return self.config.percentage[self._days[index - 1]]

    def _sun_window(self, day: datetime.date) -> tuple[float, float]:
        observer = ephem.Observer()
        observer.lat = math.radians(self.latitude)
        observer.lon = math.radians(self.longitude)
        # officiele definitie: bovenrand zon op de horizon, refractie 34'
        observer.pressure = 0
        observer.horizon = "-0:34"
        local_noon = datetime.datetime(day.year, day.month, day.day, 12).timestamp()
        observer.date = ephem.Date(
            datetime.datetime.fromtimestamp(local_noon, datetime.UTC).replace(
                tzinfo=None
            )
        )
        sun = ephem.Sun()
        try:
            rising = observer.previous_rising(sun).datetime()
            setting = observer.next_setting(sun).datetime()
        except ephem.AlwaysUpError:
            return local_noon - 12 * 3600, local_noon + 12 * 3600
        except ephem.NeverUpError:
            return local_noon, local_noon
        return (
            rising.replace(tzinfo=datetime.UTC).timestamp(),
            setting.replace(tzinfo=datetime.UTC).timestamp(),
        )

    def _fixed_window(self, day: datetime.date) -> tuple[float, float]:
        def local_ts(hhmm: str) -> float:
            t = datetime.datetime.strptime(hhmm, "%H:%M")
            return datetime.datetime(
                day.year, day.month, day.day, t.hour, t.minute
            ).timestamp()

        return local_ts(self.config.start), local_ts(self.config.end)

    def window(self, day: datetime.date) -> tuple[float, float]:
        """Begin en eind (unix timestamps) van het bonusvenster op lokale dag."""
        if day not in self._windows:
            if self.config.window == "sun":
                self._windows[day] = self._sun_window(day)
            else:
                self._windows[day] = self._fixed_window(day)
        return self._windows[day]

    def fraction(self, start_ts: float, duration_s: float) -> float:
        """Deel (0..1) van het interval dat binnen het bonusvenster valt."""
        if duration_s <= 0:
            return 0.0
        end_ts = start_ts + duration_s
        day = datetime.datetime.fromtimestamp(start_ts).date()
        overlap = 0.0
        # een interval kan over middernacht lopen
        for d in (day, day + datetime.timedelta(days=1)):
            w_start, w_end = self.window(d)
            overlap += max(0.0, min(end_ts, w_end) - max(start_ts, w_start))
        return min(1.0, overlap / duration_s)

    def bonus(
        self,
        start_ts: float,
        duration_s: float,
        market_price: float,
        supplier_cost: float,
    ) -> float:
        """Bonus in euro/kWh (excl. belastingen en btw) voor een interval."""
        day = datetime.datetime.fromtimestamp(start_ts).date()
        percentage = self.percentage(day)
        if percentage == 0:
            return 0.0
        base = market_price
        if self.config.include_supplier_cost:
            base += supplier_cost
        if self.config.only_positive_price and base <= 0:
            return 0.0
        return base * percentage / 100 * self.fraction(start_ts, duration_s)

    def eligible_kwh(
        self,
        start_ts: float,
        duration_s: float,
        kwh: float,
        market_price: float,
        supplier_cost: float,
    ) -> float:
        """Deel van de teruglevering (kWh) in een interval dat bonus krijgt."""
        if kwh <= 0:
            return 0.0
        if self.bonus(start_ts, duration_s, market_price, supplier_cost) <= 0:
            return 0.0
        return kwh * self.fraction(start_ts, duration_s)


def interval_durations(timestamps: list[float], max_s: float = 3600) -> list[float]:
    """Duur (s) van elk interval, afgeleid uit de afstand tot het volgende tijdstip."""
    result = []
    for i, ts in enumerate(timestamps):
        if i + 1 < len(timestamps):
            duration = timestamps[i + 1] - ts
        elif i > 0:
            duration = ts - timestamps[i - 1]
        else:
            duration = max_s
        result.append(min(duration, max_s))
    return result


def split_quarters(
    start_ts: float, duration_s: float, kwh: float, step_s: float = 900
) -> list[tuple[float, float, float]]:
    """
    Verdeelt een interval gelijkmatig over kwartieren
    :return: list van (begin, duur, kWh); een interval van een kwartier of korter blijft heel
    """
    count = max(1, round(duration_s / step_s))
    if count == 1:
        return [(start_ts, duration_s, kwh)]
    part = duration_s / count
    return [(start_ts + i * part, part, kwh / count) for i in range(count)]


class AnnualCapUsage:
    """Cumulatieve teruglevering met bonus binnen een kalenderjaar."""

    def __init__(
        self,
        end_timestamps: list[float],
        eligible_kwh: list[float],
        start_kwh: float = 0.0,
        reached_ts: float | None = None,
    ):
        """
        :param end_timestamps: eindtijdstippen van de intervallen, oplopend
        :param eligible_kwh: bonus-kWh per interval
        :param start_kwh: bonus-kWh van het jaar tot het eerste interval
        :param reached_ts: eindtijdstip van het interval waarin het maximum werd
            bereikt, als dat voor het eerste interval ligt
        """
        self._ends = end_timestamps
        self._start_kwh = start_kwh
        self._reached_ts = reached_ts
        self._cumulative = []
        total = start_kwh
        for kwh in eligible_kwh:
            total += kwh
            self._cumulative.append(total)

    def used_before(self, ts: float) -> float:
        """
        Verbruikte bonus-kWh van intervallen die op of voor ts eindigen;
        voor ts voor het eerste interval: start_kwh
        """
        index = bisect.bisect_right(self._ends, ts)
        if index == 0:
            return self._start_kwh
        return self._cumulative[index - 1]

    def reached_at(self, cap: float) -> float | None:
        """Eindtijdstip van het interval waarin cap werd bereikt, None als nog niet."""
        if self._reached_ts is not None:
            return self._reached_ts
        if self._start_kwh >= cap:
            return -math.inf
        index = bisect.bisect_left(self._cumulative, cap)
        if index == len(self._cumulative):
            return None
        return self._ends[index]

    def cap_reached(self, ts: float, cap: float) -> bool:
        """True als de intervallen die op of voor ts eindigen samen cap halen."""
        reached = self.reached_at(cap)
        return reached is not None and ts >= reached


class AnnualCapCounter:
    """
    Per kalenderjaar opgeslagen teller van de teruglevering met bonus, zodat een
    run alleen de teruglevering sinds de vorige run hoeft op te halen.
    Per jaar: "until" (unix timestamp tot waar geteld is), "kwh" (bonus-kWh
    voor "until") en "reached" (eindtijdstip van het interval waarin het maximum
    werd bereikt, of None). Een teller met een andere "fingerprint" (andere
    instellingen of bron) wordt niet gebruikt.
    """

    VERSION = 1

    def __init__(self, path: str, fingerprint: str):
        self.path = path
        self.fingerprint = fingerprint

    @staticmethod
    def make_fingerprint(*parts) -> str:
        text = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if (
            not isinstance(data, dict)
            or data.get("version") != self.VERSION
            or data.get("fingerprint") != self.fingerprint
            or not isinstance(data.get("years"), dict)
        ):
            return {}
        return data["years"]

    def load(self, year: int) -> dict | None:
        """De teller van year, of None als er geen bruikbare teller is."""
        state = self._read().get(str(year))
        if not isinstance(state, dict):
            return None
        try:
            until = float(state["until"])
            kwh = float(state["kwh"])
            reached = state.get("reached")
            reached = None if reached is None else float(reached)
        except (KeyError, TypeError, ValueError):
            return None
        year_start = datetime.datetime(year, 1, 1).timestamp()
        year_end = datetime.datetime(year + 1, 1, 1).timestamp()
        if not year_start <= until <= year_end or kwh < 0 or not math.isfinite(kwh):
            return None
        if reached is not None and not reached <= until:
            return None
        return {"until": until, "kwh": kwh, "reached": reached}

    def save(self, year: int, until: float, kwh: float, reached: float | None):
        years = self._read()
        years[str(year)] = {"until": until, "kwh": kwh, "reached": reached}
        # alleen het lopende en het vorige jaar bewaren
        keep = sorted(years, key=int)[-2:]
        data = {
            "version": self.VERSION,
            "fingerprint": self.fingerprint,
            "years": {y: years[y] for y in keep},
        }
        directory = os.path.dirname(self.path) or "."
        try:
            # de webserver en de scheduler kunnen tegelijk lezen en schrijven
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, self.path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError as e:
            logging.warning(f"Teller zonnebonus niet opgeslagen in {self.path}: {e}")
