"""Enriquecimiento en lote de telemetría (independiente de la base de datos).

El worker Bronce → Plata lee un lote, lo pasa por :class:`SilverEnricher` y
escribe el resultado. Mantener esta pieza sin E/S permite probar el pipeline
completo con el simulador, sin PostGIS.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from simon_geo.domain import Alert, TelemetryPoint, Vehicle
from simon_geo.parameters import PipelineParameters
from simon_geo.rules.track import EnrichedPoint, TrackProcessor
from simon_geo.spatial.geofencing import GeofenceIndex
from simon_geo.spatial.routes import PlannedRoute


@dataclass(slots=True)
class EnrichmentResult:
    points: list[EnrichedPoint] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    duplicates: int = 0
    unknown_vehicle: list[TelemetryPoint] = field(default_factory=list)


def group_and_deduplicate(
    points: Iterable[TelemetryPoint],
) -> tuple[dict[str, list[TelemetryPoint]], int]:
    """Agrupa por vehículo, ordena por tiempo y descarta retransmisiones.

    Un duplicado es otro paquete del mismo vehículo con el mismo ``event_ts``
    (misma semántica que la PK de Bronce); se conserva el primero recibido.
    """
    by_vehicle: dict[str, dict] = defaultdict(dict)
    duplicates = 0
    for p in points:
        bucket = by_vehicle[p.vehicle_id]
        if p.event_ts in bucket:
            duplicates += 1
            continue
        bucket[p.event_ts] = p
    grouped = {vid: [b[ts] for ts in sorted(b)] for vid, b in by_vehicle.items()}
    return grouped, duplicates


class SilverEnricher:
    """Mantiene un :class:`TrackProcessor` por vehículo entre lotes."""

    def __init__(
        self,
        vehicles: Iterable[Vehicle],
        geofence_index: GeofenceIndex,
        params: PipelineParameters,
        active_routes: Mapping[str, PlannedRoute] | None = None,
    ) -> None:
        self._vehicles = {v.vehicle_id: v for v in vehicles}
        self._index = geofence_index
        self._params = params
        self._routes = dict(active_routes or {})
        self._processors: dict[str, TrackProcessor] = {}

    def _processor(self, vehicle_id: str) -> TrackProcessor | None:
        if vehicle_id not in self._processors:
            vehicle = self._vehicles.get(vehicle_id)
            if vehicle is None:
                return None
            self._processors[vehicle_id] = TrackProcessor(
                vehicle, self._index, self._params, self._routes.get(vehicle_id)
            )
        return self._processors[vehicle_id]

    def enrich(
        self,
        points: Iterable[TelemetryPoint],
        geofences_by_point: Mapping[tuple[str, datetime], Sequence[int]] | None = None,
    ) -> EnrichmentResult:
        """Enriquece un lote.

        ``geofences_by_point``: geocercas resueltas en PostGIS por
        (vehicle_id, event_ts). Si se omite, se usa el índice en memoria.
        """
        grouped, duplicates = group_and_deduplicate(points)
        result = EnrichmentResult(duplicates=duplicates)
        for vehicle_id in sorted(grouped):
            processor = self._processor(vehicle_id)
            if processor is None:
                result.unknown_vehicle.extend(grouped[vehicle_id])
                continue
            for point in grouped[vehicle_id]:
                ids = (
                    geofences_by_point.get((point.vehicle_id, point.event_ts), ())
                    if geofences_by_point is not None
                    else None
                )
                out = processor.process(point, ids)
                result.points.append(out.point)
                result.alerts.extend(out.alerts)
        return result
