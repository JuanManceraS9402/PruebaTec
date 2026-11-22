"""Contratos de entrada/salida de la API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from gps_flota.domain import VEHICLE_ID_PATTERN, TelemetryPoint


class TelemetryIn(BaseModel):
    """Paquete AVL tal como lo envía el dispositivo."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "vehicle_id": "VHC-8472-ABC",
                    "event_ts": "2026-11-20T06:00:00-05:00",
                    "lat": 4.668,
                    "lon": -74.15,
                    "speed_kmh": 42.5,
                    "fuel_level_pct": 70.2,
                }
            ]
        },
    )

    vehicle_id: str = Field(pattern=VEHICLE_ID_PATTERN.pattern)
    event_ts: AwareDatetime  # sin zona horaria se rechaza: ambigüedad inaceptable
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=300)
    fuel_level_pct: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _reject_null_island(self) -> Self:
        # (0, 0) es lo que reportan muchos equipos AVL cuando aún no tienen fix GPS
        if self.lat == 0 and self.lon == 0:
            raise ValueError("Coordenadas (0, 0): el equipo no tiene fix GPS")
        return self

    def to_domain(self) -> TelemetryPoint:
        return TelemetryPoint(
            vehicle_id=self.vehicle_id,
            event_ts=self.event_ts,
            lat=self.lat,
            lon=self.lon,
            speed_kmh=self.speed_kmh,
            fuel_level_pct=self.fuel_level_pct,
        )


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packets: list[TelemetryIn] = Field(min_length=1)


class RejectedPacket(BaseModel):
    index: int
    vehicle_id: str
    event_ts: datetime
    reason: str


class IngestResponse(BaseModel):
    received: int
    inserted: int
    duplicates: int = Field(description="Retransmisiones ya almacenadas o repetidas en el lote")
    rejected: list[RejectedPacket]


class AlertOut(BaseModel):
    alert_id: int
    vehicle_id: str = Field(description="Enmascarado (VHC-****-ABC) salvo para administradores")
    vehicle_key: str | None = Field(description="Seudónimo estable para agrupar por vehículo")
    event_ts: datetime
    alert_type: str
    severity: str
    geofence_id: int | None
    lat: float
    lon: float
    observed_value: float | None
    threshold_value: float | None
    details: dict[str, Any]
