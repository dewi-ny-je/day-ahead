import datetime
import os
import time

import pytest
from pydantic import ValidationError

from dao.prog.config.models.pricing import PricingConfig, ProductionBonusConfig
from dao.prog.production_bonus import (
    AnnualCapCounter,
    AnnualCapUsage,
    ProductionBonus,
    interval_durations,
    split_quarters,
)

QUARTER = 900


@pytest.fixture(autouse=True)
def amsterdam_tz():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Amsterdam"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def ts(*args) -> float:
    return datetime.datetime(*args).timestamp()


def zonneplan(**kwargs) -> ProductionBonus:
    config = ProductionBonusConfig(
        **{"percentage": {"2025-01-01": 10}, "sun location": "nl_center", **kwargs}
    )
    return ProductionBonus(config, latitude=0, longitude=0)


class TestProductionBonusConfig:
    def test_defaults(self):
        c = ProductionBonusConfig(percentage={"2025-01-01": 10})
        assert c.window == "sun"
        assert c.sun_location == "home"
        assert c.include_supplier_cost is True
        assert c.only_positive_price is True
        assert c.annual_cap is None

    def test_aliases(self):
        c = ProductionBonusConfig(
            **{
                "percentage": {"2025-01-01": 50},
                "window": "fixed",
                "start": "06:00",
                "end": "22:00",
                "include supplier cost": False,
                "only positive price": True,
                "annual cap": 6000,
            }
        )
        assert c.window == "fixed"
        assert c.include_supplier_cost is False
        assert c.annual_cap == 6000

    def test_percentage_out_of_range(self):
        with pytest.raises(ValidationError):
            ProductionBonusConfig(percentage={"2025-01-01": 110})

    def test_percentage_bad_date(self):
        with pytest.raises(ValidationError):
            ProductionBonusConfig(percentage={"01-01-2025": 10})

    def test_percentage_empty(self):
        with pytest.raises(ValidationError):
            ProductionBonusConfig(percentage={})

    def test_bad_time(self):
        with pytest.raises(ValidationError):
            ProductionBonusConfig(percentage={"2025-01-01": 10}, start="6 uur")

    def test_end_before_start(self):
        with pytest.raises(ValidationError):
            ProductionBonusConfig(
                percentage={"2025-01-01": 10}, start="22:00", end="06:00"
            )

    def test_end_compared_as_time(self):
        c = ProductionBonusConfig(percentage={"2025-01-01": 10}, start="6:00", end="22:00")
        assert c.end == "22:00"
        with pytest.raises(ValidationError):
            ProductionBonusConfig(percentage={"2025-01-01": 10}, start="9:00", end="8:30")

    def test_percentage_keys_normalised(self):
        c = ProductionBonusConfig(percentage={"2025-1-1": 10, "2025-06-01": 0})
        assert c.percentage == {"2025-01-01": 10, "2025-06-01": 0}

    def test_negative_cap(self):
        with pytest.raises(ValidationError):
            ProductionBonusConfig(
                **{"percentage": {"2025-01-01": 10}, "annual cap": -1}
            )

    def test_optional_in_pricing(self):
        base = {
            "energy taxes consumption": {"2025-01-01": 0.1},
            "energy taxes production": {"2025-01-01": 0.1},
            "cost supplier consumption": {"2025-01-01": 0.02},
            "cost supplier production": {"2025-01-01": 0.02},
            "vat consumption": {"2025-01-01": 21},
            "vat production": {"2025-01-01": 21},
            "last invoice": "2025-01-01",
        }
        assert PricingConfig(**base).production_bonus is None
        pricing = PricingConfig(
            **base, **{"production bonus": {"percentage": {"2025-01-01": 10}}}
        )
        assert pricing.production_bonus.percentage == {"2025-01-01": 10}


class TestPercentage:
    def test_before_first_date(self):
        assert zonneplan().percentage(datetime.date(2024, 12, 31)) == 0

    def test_date_keyed(self):
        bonus = zonneplan(percentage={"2025-01-01": 10, "2026-07-01": 15})
        assert bonus.percentage(datetime.date(2026, 6, 30)) == 10
        assert bonus.percentage(datetime.date(2026, 7, 1)) == 15

    def test_unpadded_date_key(self):
        bonus = zonneplan(percentage={"2025-1-1": 10, "2025-10-1": 0})
        assert bonus.percentage(datetime.date(2025, 3, 1)) == 10
        assert bonus.percentage(datetime.date(2025, 10, 1)) == 0


class TestSunWindow:
    def test_de_bilt_midsummer(self):
        # zonsopkomst/-ondergang De Bilt 21 juni 2026: ca. 05:17 en 22:03 lokale tijd
        rise, set_ = zonneplan().window(datetime.date(2026, 6, 21))
        assert abs(rise - ts(2026, 6, 21, 5, 17)) < 180
        assert abs(set_ - ts(2026, 6, 21, 22, 3)) < 180

    def test_de_bilt_midwinter(self):
        # ca. 08:46 en 16:28 lokale tijd
        rise, set_ = zonneplan().window(datetime.date(2026, 12, 21))
        assert abs(rise - ts(2026, 12, 21, 8, 46)) < 180
        assert abs(set_ - ts(2026, 12, 21, 16, 28)) < 180

    def test_home_location_differs(self):
        config = ProductionBonusConfig(percentage={"2025-01-01": 10})
        groningen = ProductionBonus(config, latitude=53.22, longitude=6.57)
        rise_home, _ = groningen.window(datetime.date(2026, 6, 21))
        rise_center, _ = zonneplan().window(datetime.date(2026, 6, 21))
        assert rise_home != rise_center


class TestFraction:
    def test_midday_full(self):
        assert zonneplan().fraction(ts(2026, 6, 21, 12), QUARTER) == 1

    def test_night_zero(self):
        assert zonneplan().fraction(ts(2026, 6, 21, 2), QUARTER) == 0

    def test_partial_hour_at_sunrise(self):
        bonus = zonneplan()
        rise, _ = bonus.window(datetime.date(2026, 6, 21))
        hour_start = rise - (rise % 3600)
        expected = (hour_start + 3600 - rise) / 3600
        assert bonus.fraction(hour_start, 3600) == pytest.approx(expected)

    def test_fixed_window(self):
        bonus = zonneplan(window="fixed", start="06:00", end="22:00")
        assert bonus.fraction(ts(2026, 1, 15, 5, 45), QUARTER) == 0
        assert bonus.fraction(ts(2026, 1, 15, 6, 0), QUARTER) == 1
        assert bonus.fraction(ts(2026, 1, 15, 21, 45), QUARTER) == 1
        assert bonus.fraction(ts(2026, 1, 15, 22, 0), QUARTER) == 0
        assert bonus.fraction(ts(2026, 1, 15, 5, 30), 3600) == pytest.approx(0.5)


class TestBonus:
    def test_zonneplan_formula(self):
        # (0,10 + 0,02) * 10% = 0,012 euro/kWh
        assert zonneplan().bonus(
            ts(2026, 6, 21, 12), QUARTER, 0.10, 0.02
        ) == pytest.approx(0.012)

    def test_negative_base_no_bonus(self):
        assert zonneplan().bonus(ts(2026, 6, 21, 12), QUARTER, -0.05, 0.02) == 0

    def test_zero_base_no_bonus(self):
        assert zonneplan().bonus(ts(2026, 6, 21, 12), QUARTER, -0.02, 0.02) == 0

    def test_small_negative_price_positive_base(self):
        # marktprijs -0,01 + 0,02 = 0,01 > 0: wel bonus
        assert zonneplan().bonus(
            ts(2026, 6, 21, 12), QUARTER, -0.01, 0.02
        ) == pytest.approx(0.001)

    def test_negative_allowed(self):
        bonus = zonneplan(**{"only positive price": False})
        assert bonus.bonus(
            ts(2026, 6, 21, 12), QUARTER, -0.12, 0.02
        ) == pytest.approx(-0.01)

    def test_exclude_supplier_cost(self):
        bonus = zonneplan(
            **{"percentage": {"2025-01-01": 50}, "include supplier cost": False}
        )
        assert bonus.bonus(
            ts(2026, 6, 21, 12), QUARTER, 0.10, 0.02
        ) == pytest.approx(0.05)

    def test_night_no_bonus(self):
        assert zonneplan().bonus(ts(2026, 6, 21, 2), QUARTER, 0.10, 0.02) == 0

    def test_before_start_date_no_bonus(self):
        assert zonneplan().bonus(ts(2024, 6, 21, 12), QUARTER, 0.10, 0.02) == 0


class TestEligibleKwh:
    def test_day(self):
        assert zonneplan().eligible_kwh(
            ts(2026, 6, 21, 12), QUARTER, 1.5, 0.10, 0.02
        ) == pytest.approx(1.5)

    def test_negative_price(self):
        assert (
            zonneplan().eligible_kwh(ts(2026, 6, 21, 12), QUARTER, 1.5, -0.10, 0.02)
            == 0
        )

    def test_night(self):
        assert (
            zonneplan().eligible_kwh(ts(2026, 6, 21, 2), QUARTER, 1.5, 0.10, 0.02)
            == 0
        )


class TestHelpers:
    def test_interval_durations(self):
        assert interval_durations([0, 900, 1800]) == [900, 900, 900]
        assert interval_durations([0, 3600, 4500]) == [3600, 900, 900]
        # gat in de data wordt begrensd op een uur
        assert interval_durations([0, 10800]) == [3600, 3600]
        assert interval_durations([0]) == [3600]
        assert interval_durations([]) == []

    def test_split_hour_into_quarters(self):
        parts = split_quarters(0, 3600, 2.0)
        assert parts == [(0, 900, 0.5), (900, 900, 0.5), (1800, 900, 0.5), (2700, 900, 0.5)]

    def test_split_quarter_unchanged(self):
        assert split_quarters(0, 900, 1.2) == [(0, 900, 1.2)]

    def test_split_short_interval_unchanged(self):
        assert split_quarters(0, 300, 0.1) == [(0, 300, 0.1)]

    def test_split_mixed_price_hour(self):
        # één negatief kwartier in een uur: alleen 3/4 van de teruglevering telt mee
        bonus = zonneplan()
        hour = ts(2026, 6, 21, 13)
        prices = [0.10, -0.05, 0.10, 0.10]
        eligible = sum(
            bonus.eligible_kwh(t, d, kwh, price, 0.02)
            for (t, d, kwh), price in zip(split_quarters(hour, 3600, 4.0), prices, strict=False)
        )
        assert eligible == pytest.approx(3.0)

    def test_annual_cap_usage(self):
        usage = AnnualCapUsage([900, 1800, 2700], [1.0, 0.0, 2.5])
        assert usage.used_before(0) == 0
        assert usage.used_before(900) == 1.0
        assert usage.used_before(2000) == 1.0
        assert usage.used_before(1e12) == 3.5
        assert AnnualCapUsage([], []).used_before(1e12) == 0

    def test_annual_cap_usage_start_kwh(self):
        usage = AnnualCapUsage([900, 1800], [1.0, 2.0], start_kwh=10.0)
        assert usage.used_before(0) == 10.0
        assert usage.used_before(900) == 11.0
        assert usage.used_before(1e12) == 13.0

    def test_cap_reached_matches_used_before(self):
        usage = AnnualCapUsage([900, 1800, 2700], [1.0, 0.0, 2.5], start_kwh=0.5)
        for cap in (0, 0.5, 1.0, 1.5, 4.0, 5.0):
            for t in (0, 899, 900, 1800, 2699, 2700, 1e12):
                assert usage.cap_reached(t, cap) == (usage.used_before(t) >= cap)

    def test_cap_reached_before_first_interval(self):
        usage = AnnualCapUsage([5000], [1.0], start_kwh=8.0, reached_ts=1200)
        assert usage.reached_at(7.5) == 1200
        assert not usage.cap_reached(1199, 7.5)
        assert usage.cap_reached(1200, 7.5)


class TestAnnualCapCounter:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "counter.json")
        counter = AnnualCapCounter(path, "abc")
        assert counter.load(2026) is None
        until = ts(2026, 3, 1)
        counter.save(2026, until, 1234.567891234, ts(2026, 2, 1))
        state = AnnualCapCounter(path, "abc").load(2026)
        assert state == {"until": until, "kwh": 1234.567891234, "reached": ts(2026, 2, 1)}
        assert AnnualCapCounter(path, "abc").load(2025) is None

    def test_other_fingerprint_ignored(self, tmp_path):
        path = str(tmp_path / "counter.json")
        AnnualCapCounter(path, "abc").save(2026, ts(2026, 3, 1), 10.0, None)
        assert AnnualCapCounter(path, "other").load(2026) is None

    def test_until_outside_year_ignored(self, tmp_path):
        path = str(tmp_path / "counter.json")
        AnnualCapCounter(path, "abc").save(2026, ts(2027, 3, 1), 10.0, None)
        assert AnnualCapCounter(path, "abc").load(2026) is None

    def test_corrupt_file_ignored(self, tmp_path):
        path = tmp_path / "counter.json"
        path.write_text("{not json")
        counter = AnnualCapCounter(str(path), "abc")
        assert counter.load(2026) is None
        counter.save(2026, ts(2026, 3, 1), 10.0, None)
        assert counter.load(2026)["kwh"] == 10.0

    def test_keeps_two_years(self, tmp_path):
        path = str(tmp_path / "counter.json")
        counter = AnnualCapCounter(path, "abc")
        for year in (2024, 2025, 2026):
            counter.save(year, ts(year, 3, 1), float(year), None)
        assert counter.load(2024) is None
        assert counter.load(2025)["kwh"] == 2025.0
        assert counter.load(2026)["kwh"] == 2026.0

    def test_fingerprint(self):
        a = AnnualCapCounter.make_fingerprint({"cap": 7500}, 52.1, {"db": "prod"})
        b = AnnualCapCounter.make_fingerprint({"cap": 7000}, 52.1, {"db": "prod"})
        assert a != b
        assert a == AnnualCapCounter.make_fingerprint({"cap": 7500}, 52.1, {"db": "prod"})
