from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from shapely.geometry import Polygon, box

from gps_flota.domain import Geofence, TelemetryPoint, Vehicle, ZoneType
from gps_flota.parameters import PipelineParameters

T0 = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)  # 06:00 en Bogotá


def square(lon: float, lat: float, half_deg: float) -> Polygon:
    return box(lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def make_geofence(
    gid: int,
    zone: ZoneType,
    limit: int | None,
    geometry: Polygon,
    code: str | None = None,
) -> Geofence:
    return Geofence(gid, code or f"GF-{gid}", f"Geocerca {gid}", zone, limit, geometry)


@pytest.fixture
def params() -> PipelineParameters:
    return PipelineParameters()


@pytest.fixture
def vehicle() -> Vehicle:
    return Vehicle("VHC-8472-ABC", fuel_tank_capacity_l=100, nominal_consumption_l_100km=30)


@pytest.fixture
def point_factory() -> Callable[..., TelemetryPoint]:
    def _make(
        seconds: float = 0,
        lat: float = 4.65,
        lon: float = -74.08,
        speed: float = 40.0,
        fuel: float = 50.0,
        vehicle_id: str = "VHC-8472-ABC",
    ) -> TelemetryPoint:
        return TelemetryPoint(
            vehicle_id=vehicle_id,
            event_ts=T0 + timedelta(seconds=seconds),
            lat=lat,
            lon=lon,
            speed_kmh=speed,
            fuel_level_pct=fuel,
        )

    return _make
