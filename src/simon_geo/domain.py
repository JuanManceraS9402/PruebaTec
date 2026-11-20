"""Modelos de dominio inmutables y validados en construcción."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

VEHICLE_ID_PATTERN = re.compile(r"^[A-Z]{3}-[0-9]{4}-[A-Z]{3}$")


class ZoneType(StrEnum):
    URBAN = "URBAN"
    SCHOOL = "SCHOOL"
    HIGH_RISK = "HIGH_RISK"
    HIGHWAY = "HIGHWAY"
    DEPOT = "DEPOT"
    RESTRICTED = "RESTRICTED"


class AlertType(StrEnum):
    SPEEDING = "SPEEDING"
    LOW_FUEL_RANGE = "LOW_FUEL_RANGE"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_ORDER.index(self)

    def escalate(self, levels: int = 1) -> Severity:
        """Sube ``levels`` niveles sin pasar de CRITICAL."""
        return _SEVERITY_ORDER[min(self.rank + levels, len(_SEVERITY_ORDER) - 1)]


_SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
)


@dataclass(frozen=True, slots=True)
class Vehicle:
    vehicle_id: str
    fuel_tank_capacity_l: float
    nominal_consumption_l_100km: float
    vehicle_type: str = "TRUCK"

    def __post_init__(self) -> None:
        if not VEHICLE_ID_PATTERN.match(self.vehicle_id):
            raise ValueError(f"ID de vehículo inválido: {self.vehicle_id!r}")
        if self.fuel_tank_capacity_l <= 0 or self.nominal_consumption_l_100km <= 0:
            raise ValueError("Capacidad de tanque y consumo nominal deben ser positivos")


@dataclass(frozen=True, slots=True)
class Geofence:
    geofence_id: int
    code: str
    name: str
    zone_type: ZoneType
    speed_limit_kmh: int | None
    geometry: BaseGeometry = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.geometry, Polygon | MultiPolygon):
            raise ValueError(f"La geocerca {self.code} debe ser Polygon o MultiPolygon")
        if self.geometry.is_empty or not self.geometry.is_valid:
            raise ValueError(f"Geometría inválida o vacía en la geocerca {self.code}")
        if self.speed_limit_kmh is not None and not 5 <= self.speed_limit_kmh <= 130:
            raise ValueError(f"Límite fuera de rango en la geocerca {self.code}")


@dataclass(frozen=True, slots=True)
class TelemetryPoint:
    """Paquete AVL tal como lo emite el dispositivo."""

    vehicle_id: str
    event_ts: datetime
    lat: float
    lon: float
    speed_kmh: float
    fuel_level_pct: float

    def __post_init__(self) -> None:
        if self.event_ts.tzinfo is None:
            raise ValueError("event_ts debe tener zona horaria")
        if not -90 <= self.lat <= 90 or not -180 <= self.lon <= 180:
            raise ValueError(f"Coordenadas fuera de rango: lat={self.lat}, lon={self.lon}")
        if not 0 <= self.speed_kmh <= 300:
            raise ValueError(f"Velocidad fuera de rango: {self.speed_kmh}")
        if not 0 <= self.fuel_level_pct <= 100:
            raise ValueError(f"Nivel de combustible fuera de rango: {self.fuel_level_pct}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "event_ts": self.event_ts.isoformat(),
            "lat": self.lat,
            "lon": self.lon,
            "speed_kmh": self.speed_kmh,
            "fuel_level_pct": self.fuel_level_pct,
        }


@dataclass(frozen=True, slots=True)
class Alert:
    vehicle_id: str
    event_ts: datetime
    alert_type: AlertType
    severity: Severity
    lon: float
    lat: float
    observed_value: float
    threshold_value: float
    geofence_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
