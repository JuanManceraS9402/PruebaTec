"""Rutas planificadas: progreso y distancia restante."""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import LineString, Point

from simon_geo.spatial.crs import lonlat_to_metric, metric_to_lonlat, to_metric


@dataclass(frozen=True)
class PlannedRoute:
    route_id: int
    code: str
    name: str
    geometry: LineString = field(repr=False)  # WGS84
    _metric: LineString = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.geometry, LineString) or len(self.geometry.coords) < 2:
            raise ValueError(f"La ruta {self.code} debe ser un LineString de al menos 2 puntos")
        if not self.geometry.is_valid:
            raise ValueError(f"Geometría inválida en la ruta {self.code}")
        object.__setattr__(self, "_metric", to_metric(self.geometry))

    @property
    def metric_geometry(self) -> LineString:
        return self._metric

    @property
    def length_km(self) -> float:
        return self._metric.length / 1000

    def progress_m(self, lon: float, lat: float) -> float:
        """Distancia recorrida sobre la ruta hasta la proyección del punto."""
        return self._metric.project(Point(lonlat_to_metric(lon, lat)))

    def remaining_km(self, lon: float, lat: float) -> float:
        """Distancia que falta para terminar la ruta.

        = tramo de ruta desde la proyección del punto hasta el final
        + distancia perpendicular a la ruta (si el vehículo se desvió, primero
          tiene que volver a ella).

        Limitación conocida: en rutas que se cruzan o regresan sobre sí mismas la
        proyección al punto más cercano es ambigua; se resolvería restringiendo
        la búsqueda a partir del último progreso conocido.
        """
        point = Point(lonlat_to_metric(lon, lat))
        along = self._metric.length - self._metric.project(point)
        off_route = self._metric.distance(point)
        return (along + off_route) / 1000

    def point_at_m(self, distance_m: float) -> tuple[float, float]:
        """(lon, lat) a ``distance_m`` metros del inicio (se recorta a los extremos)."""
        p = self._metric.interpolate(min(max(distance_m, 0.0), self._metric.length))
        return metric_to_lonlat(p.x, p.y)
