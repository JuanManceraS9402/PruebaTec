from __future__ import annotations

import random

import pytest
from shapely.geometry import LineString

from simon_geo.domain import Severity
from simon_geo.rules.fuel import (
    ConsumptionEstimator,
    ConsumptionSource,
    assess_fuel_risk,
    estimate_range_km,
    is_refuel,
)
from simon_geo.spatial.routes import PlannedRoute


def make_estimator(tank: float = 200, nominal: float = 30) -> ConsumptionEstimator:
    return ConsumptionEstimator(tank, nominal, window_km=30, min_sample_km=3)


def feed(
    est: ConsumptionEstimator,
    *,
    true_l_100km: float,
    tank: float,
    km: float,
    start_pct: float = 90,
    step_km: float = 0.05,
    noise: float = 0.0,
    seed: int = 1,
) -> None:
    rng = random.Random(seed)
    pct_per_km = true_l_100km / 100 / tank * 100
    n = int(km / step_km)
    for i in range(n + 1):
        odo = i * step_km
        est.add(odo, start_pct - pct_per_km * odo + rng.gauss(0, noise))


class TestRefuel:
    def test_jump_above_threshold_is_refuel(self) -> None:
        assert is_refuel(12.0, 90.0, min_jump_pct=5)

    def test_sensor_noise_is_not_refuel(self) -> None:
        assert not is_refuel(40.0, 40.4, min_jump_pct=5)

    def test_consumption_is_not_refuel(self) -> None:
        assert not is_refuel(40.0, 39.0, min_jump_pct=5)


class TestConsumptionEstimator:
    def test_uses_nominal_without_enough_distance(self) -> None:
        est = make_estimator()
        feed(est, true_l_100km=40, tank=200, km=2)  # < min_sample_km
        assert est.estimate().source is ConsumptionSource.NOMINAL
        assert est.estimate().l_100km == 30

    def test_recovers_true_consumption_exactly_without_noise(self) -> None:
        est = make_estimator()
        feed(est, true_l_100km=36, tank=200, km=10)
        estimate = est.estimate()
        assert estimate.source is ConsumptionSource.MEASURED
        assert estimate.l_100km == pytest.approx(36, rel=1e-6)

    def test_robust_to_sensor_noise(self) -> None:
        est = make_estimator()
        feed(est, true_l_100km=36, tank=200, km=25, noise=0.15)
        assert est.estimate().l_100km == pytest.approx(36, rel=0.05)

    def test_window_is_bounded(self) -> None:
        est = make_estimator()
        feed(est, true_l_100km=36, tank=200, km=100)
        assert 30 <= est.span_km <= 30.1

    def test_flat_fuel_reading_falls_back_to_nominal(self) -> None:
        est = make_estimator()
        for i in range(100):
            est.add(i * 0.1, 50.0)  # sensor pegado → consumo 0, implausible
        assert est.estimate().source is ConsumptionSource.NOMINAL

    def test_reset_clears_history(self) -> None:
        est = make_estimator()
        feed(est, true_l_100km=36, tank=200, km=10)
        est.reset()
        assert est.estimate().source is ConsumptionSource.NOMINAL

    def test_odometer_cannot_go_backwards(self) -> None:
        est = make_estimator()
        est.add(5.0, 50)
        with pytest.raises(ValueError):
            est.add(4.9, 50)


class TestRange:
    def test_range_from_fuel_level(self) -> None:
        # 50 % de 200 L = 100 L a 25 L/100 km = 400 km
        assert estimate_range_km(50, 200, 25) == pytest.approx(400)

    def test_empty_tank_has_zero_range(self) -> None:
        assert estimate_range_km(0, 200, 25) == 0

    def test_invalid_consumption(self) -> None:
        with pytest.raises(ValueError):
            estimate_range_km(50, 200, 0)


class TestRiskAssessment:
    def test_cannot_reach_destination_is_critical(self) -> None:
        r = assess_fuel_risk(range_km=9, remaining_km=10, safety_factor=1.15)
        assert r.is_risk and r.severity is Severity.CRITICAL

    def test_reaches_without_margin_is_high(self) -> None:
        r = assess_fuel_risk(range_km=11, remaining_km=10, safety_factor=1.15)
        assert r.is_risk and r.severity is Severity.HIGH
        assert r.required_km == pytest.approx(11.5)

    def test_enough_fuel_is_no_risk(self) -> None:
        r = assess_fuel_risk(range_km=12, remaining_km=10, safety_factor=1.15)
        assert not r.is_risk and r.severity is None

    def test_at_destination_is_no_risk(self) -> None:
        assert not assess_fuel_risk(range_km=0.5, remaining_km=0, safety_factor=1.15).is_risk


class TestRemainingRoute:
    # Ruta recta norte-sur de ~11,06 km en Bogotá
    ROUTE = PlannedRoute(1, "T", "Test", LineString([(-74.10, 4.60), (-74.10, 4.70)]))

    def test_route_length_in_meters(self) -> None:
        assert self.ROUTE.length_km == pytest.approx(11.06, rel=0.01)

    def test_at_start_remaining_is_full_length(self) -> None:
        assert self.ROUTE.remaining_km(-74.10, 4.60) == pytest.approx(self.ROUTE.length_km)

    def test_halfway(self) -> None:
        assert self.ROUTE.remaining_km(-74.10, 4.65) == pytest.approx(
            self.ROUTE.length_km / 2, rel=0.01
        )

    def test_off_route_adds_return_distance(self) -> None:
        on = self.ROUTE.remaining_km(-74.10, 4.65)
        off = self.ROUTE.remaining_km(-74.09, 4.65)  # ~1,1 km al oriente
        assert off - on == pytest.approx(1.11, rel=0.02)

    def test_past_the_end_is_zero_plus_offset(self) -> None:
        assert self.ROUTE.remaining_km(-74.10, 4.70) == pytest.approx(0, abs=1e-6)
