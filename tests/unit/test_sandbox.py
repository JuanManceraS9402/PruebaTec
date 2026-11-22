"""Simulación bajo demanda: validación y generación de telemetría, sin base de datos."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gps_flota.config import Settings
from gps_flota.sandbox import SandboxError, SandboxRequest, check_time_window, simulate_points
from gps_flota.seed import bogota
from gps_flota.spatial.geofencing import GeofenceIndex

START = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)
ROUTES = bogota.routes()
INDEX = GeofenceIndex(bogota.geofences())


def request(**overrides) -> SandboxRequest:
    values = {
        "vehicle_id": "VHC-1234-XYZ",
        "route_code": "R04",
        "profile": "aggressive",
        "fuel_pct": 60.0,
        "tank_l": 150.0,
        "consumption_l_100km": 30.0,
        "start": START,
    }
    return SandboxRequest(**{**values, **overrides})


@pytest.mark.parametrize(
    "bad",
    [
        {"vehicle_id": "ABC123"},
        {"profile": "kamikaze"},
        {"fuel_pct": 0.0},
        {"fuel_pct": 101.0},
        {"tank_l": 0.0},
        {"consumption_l_100km": -1.0},
        {"start": datetime(2026, 11, 20, 8, 0)},
    ],
)
def test_invalid_requests_are_rejected(bad: dict) -> None:
    with pytest.raises(SandboxError):
        request(**bad)


def test_vehicle_is_marked_as_sandbox() -> None:
    assert request().vehicle.vehicle_type == "SANDBOX"


def test_simulation_follows_request_and_is_deterministic() -> None:
    points = simulate_points(request(), ROUTES, INDEX)
    assert points == simulate_points(request(), ROUTES, INDEX)
    assert {p.vehicle_id for p in points} == {"VHC-1234-XYZ"}
    assert min(p.event_ts for p in points) == START


def test_unknown_route_is_rejected() -> None:
    with pytest.raises(SandboxError, match="R99"):
        simulate_points(replace(request(), route_code="R99"), ROUTES, INDEX)


def test_time_window_matches_api_rules() -> None:
    settings = Settings(_env_file=None)
    points = simulate_points(request(), ROUTES, INDEX)
    last = max(p.event_ts for p in points)
    check_time_window(points, last, settings)
    with pytest.raises(SandboxError, match="futuro"):
        check_time_window(points, last - timedelta(hours=1), settings)
    with pytest.raises(SandboxError, match="días"):
        check_time_window(points, START + timedelta(days=31), settings)
