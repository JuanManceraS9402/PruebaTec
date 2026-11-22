"""Rutas nuevas bajo demanda: de dos puntos en el mapa a una línea sobre la red vial.

El recorrido se calcula con OSRM sobre OpenStreetMap (ODbL). Es un servicio público de
demostración: solo se le envían las coordenadas de inicio y fin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx
from shapely.geometry import LineString

from simon_geo.spatial.crs import to_metric, to_wgs84
from simon_geo.spatial.routes import PlannedRoute

OSRM_URL: Final = "https://router.project-osrm.org/route/v1/driving/"
USER_AGENT: Final = "simon-geo-demo/0.1"
# Bogotá y municipios vecinos (lon_min, lat_min, lon_max, lat_max)
SERVICE_AREA: Final = (-74.35, 4.40, -73.95, 4.90)
MIN_ROUTE_KM: Final = 0.5
MAX_ROUTE_KM: Final = 80.0
SIMPLIFY_M: Final = 5.0


class RoutingError(Exception):
    """Error explicable al usuario del tablero."""


@dataclass(frozen=True, slots=True)
class Place:
    lon: float
    lat: float

    @property
    def label(self) -> str:
        return f"{self.lat:.4f}, {self.lon:.4f}"


@dataclass(frozen=True, slots=True)
class RoutePlan:
    line: LineString
    name: str
    length_km: float

    def planned(self, code: str) -> PlannedRoute:
        return PlannedRoute(0, code, self.name, self.line)


def in_service_area(place: Place) -> bool:
    lon_min, lat_min, lon_max, lat_max = SERVICE_AREA
    return lon_min <= place.lon <= lon_max and lat_min <= place.lat <= lat_max


def next_points(points: list[Place], click: Place) -> list[Place]:
    """Primer clic = inicio, segundo = fin, el siguiente empieza una ruta nueva."""
    return [points[0], click] if len(points) == 1 else [click]


def plan_route(
    origin: Place, destination: Place, *, client: httpx.Client | None = None
) -> RoutePlan:
    """Recorrido por las vías reales; se nombra con las vías de inicio y fin."""
    for place in (origin, destination):
        if not in_service_area(place):
            raise RoutingError(f"El punto {place.label} está fuera del área de Bogotá")

    owns_client = client is None
    client = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
    url = f"{OSRM_URL}{origin.lon},{origin.lat};{destination.lon},{destination.lat}"
    try:
        response = client.get(url, params={"overview": "full", "geometries": "geojson"})
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RoutingError("El servicio de rutas no responde: usa una ruta existente") from exc
    finally:
        if owns_client:
            client.close()

    if body.get("code") != "Ok" or not body.get("routes"):
        raise RoutingError("No hay un recorrido por vías entre esos dos puntos")
    coords = body["routes"][0]["geometry"]["coordinates"]
    if len(coords) < 2:
        raise RoutingError("El inicio y el fin están en el mismo punto")

    metric = to_metric(LineString(coords))
    length_km = metric.length / 1000
    if length_km < MIN_ROUTE_KM:
        raise RoutingError(f"La ruta es muy corta (mínimo {MIN_ROUTE_KM} km)")
    if length_km > MAX_ROUTE_KM:
        raise RoutingError(f"La ruta es muy larga (máximo {MAX_ROUTE_KM:.0f} km)")

    waypoints = body.get("waypoints") or [{}, {}]
    start = waypoints[0].get("name") or origin.label
    end = waypoints[-1].get("name") or destination.label
    simplified = to_wgs84(metric.simplify(SIMPLIFY_M, preserve_topology=True))
    return RoutePlan(simplified, f"{start} → {end}", round(length_km, 1))
