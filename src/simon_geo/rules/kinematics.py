"""Cinemática entre reportes consecutivos: distancia, outliers GPS y eventos bruscos."""

from __future__ import annotations

from dataclasses import dataclass

from simon_geo.domain import TelemetryPoint
from simon_geo.parameters import PipelineParameters
from simon_geo.spatial.distance import haversine_m


@dataclass(frozen=True, slots=True)
class SegmentMetrics:
    delta_s: float | None
    distance_m: float
    implied_speed_kmh: float | None
    accel_mps2: float | None
    is_gps_outlier: bool
    is_harsh_event: bool

    @classmethod
    def first_point(cls) -> SegmentMetrics:
        return cls(
            delta_s=None,
            distance_m=0.0,
            implied_speed_kmh=None,
            accel_mps2=None,
            is_gps_outlier=False,
            is_harsh_event=False,
        )


def segment_metrics(
    prev: TelemetryPoint | None, curr: TelemetryPoint, params: PipelineParameters
) -> SegmentMetrics:
    """Métricas del tramo ``prev → curr``.

    ``prev`` debe ser el último punto VÁLIDO del vehículo: si fuera un outlier,
    el punto correcto siguiente parecería otro salto.

    * Outlier: la velocidad implícita (distancia / tiempo) es físicamente imposible.
    * Aceleración: a partir de las velocidades reportadas, solo si el hueco entre
      reportes es corto (con huecos largos el promedio no representa un evento).
    """
    if prev is None:
        return SegmentMetrics.first_point()

    delta_s = (curr.event_ts - prev.event_ts).total_seconds()
    if delta_s <= 0:
        raise ValueError(
            f"Puntos fuera de orden o duplicados para {curr.vehicle_id}: "
            f"{prev.event_ts.isoformat()} → {curr.event_ts.isoformat()}"
        )

    distance_m = haversine_m(prev.lon, prev.lat, curr.lon, curr.lat)
    implied_speed_kmh = distance_m / delta_s * 3.6
    is_outlier = implied_speed_kmh > params.gps_max_implied_speed_kmh

    accel: float | None = None
    harsh = False
    if not is_outlier and delta_s <= params.accel_max_gap_s:
        accel = (curr.speed_kmh - prev.speed_kmh) / 3.6 / delta_s
        harsh = accel >= params.harsh_accel_mps2 or accel <= -params.harsh_brake_mps2

    return SegmentMetrics(
        delta_s=delta_s,
        distance_m=distance_m,
        implied_speed_kmh=implied_speed_kmh,
        accel_mps2=accel,
        is_gps_outlier=is_outlier,
        is_harsh_event=harsh,
    )
