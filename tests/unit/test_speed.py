from __future__ import annotations

import pytest

from gps_flota.domain import Severity, ZoneType
from gps_flota.rules.speed import base_severity, evaluate_speed
from gps_flota.spatial.geofencing import SpeedLimitDecision
from tests.conftest import make_geofence, square

URBAN_50 = SpeedLimitDecision(50, make_geofence(1, ZoneType.URBAN, 50, square(0, 0, 1)))
SCHOOL_30 = SpeedLimitDecision(30, make_geofence(2, ZoneType.SCHOOL, 30, square(0, 0, 1)))
DEFAULT_80 = SpeedLimitDecision(80, None)


def test_under_limit_is_not_speeding() -> None:
    ev = evaluate_speed(45, URBAN_50, tolerance_kmh=3)
    assert not ev.is_speeding
    assert ev.excess_kmh == 0
    assert ev.severity is None


def test_within_tolerance_is_not_speeding_but_reports_excess() -> None:
    ev = evaluate_speed(53, URBAN_50, tolerance_kmh=3)
    assert not ev.is_speeding
    assert ev.excess_kmh == pytest.approx(3)


def test_just_over_tolerance_is_speeding() -> None:
    ev = evaluate_speed(53.1, URBAN_50, tolerance_kmh=3)
    assert ev.is_speeding
    assert ev.excess_kmh == pytest.approx(3.1)
    assert ev.severity is Severity.LOW


@pytest.mark.parametrize(
    ("excess", "expected"),
    [
        (5, Severity.LOW),
        (9.9, Severity.LOW),
        (10, Severity.MEDIUM),
        (19.9, Severity.MEDIUM),
        (20, Severity.HIGH),
        (30, Severity.CRITICAL),
        (80, Severity.CRITICAL),
    ],
)
def test_severity_bands(excess: float, expected: Severity) -> None:
    assert base_severity(excess) is expected


def test_same_excess_is_more_severe_in_school_zone() -> None:
    urban = evaluate_speed(50 + 15, URBAN_50, tolerance_kmh=3)
    school = evaluate_speed(30 + 15, SCHOOL_30, tolerance_kmh=3)
    assert urban.severity is Severity.MEDIUM
    assert school.severity is Severity.HIGH


def test_escalation_is_capped_at_critical() -> None:
    ev = evaluate_speed(30 + 50, SCHOOL_30, tolerance_kmh=3)
    assert ev.severity is Severity.CRITICAL


def test_default_limit_applies_outside_geofences() -> None:
    assert not evaluate_speed(82, DEFAULT_80, tolerance_kmh=3).is_speeding
    assert evaluate_speed(90, DEFAULT_80, tolerance_kmh=3).is_speeding


def test_negative_speed_rejected() -> None:
    with pytest.raises(ValueError):
        evaluate_speed(-1, URBAN_50, tolerance_kmh=3)
