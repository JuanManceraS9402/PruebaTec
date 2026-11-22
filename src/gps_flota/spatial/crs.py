"""Sistemas de referencia y constructores de geometría.

Se almacena en WGS84 (EPSG:4326, lo que emiten los GPS) y se calcula en
MAGNA-SIRGAS / Origen-Nacional (EPSG:9377), proyección oficial de Colombia en
metros: buffers, longitudes y proyecciones sobre rutas quedan en metros reales.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Final, TypeVar

import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

WGS84: Final = "EPSG:4326"
COLOMBIA_METRIC: Final = "EPSG:9377"

G = TypeVar("G", bound=BaseGeometry)


@lru_cache(maxsize=1)
def _forward() -> Transformer:
    return Transformer.from_crs(WGS84, COLOMBIA_METRIC, always_xy=True)


@lru_cache(maxsize=1)
def _inverse() -> Transformer:
    return Transformer.from_crs(COLOMBIA_METRIC, WGS84, always_xy=True)


def to_metric(geom: G) -> G:
    """WGS84 (lon, lat) → EPSG:9377 (metros)."""
    return shapely.transform(geom, _forward().transform, interleaved=False)


def to_wgs84(geom: G) -> G:
    """EPSG:9377 (metros) → WGS84 (lon, lat)."""
    return shapely.transform(geom, _inverse().transform, interleaved=False)


def lonlat_to_metric(lon: float, lat: float) -> tuple[float, float]:
    return _forward().transform(lon, lat)


def metric_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return _inverse().transform(x, y)


def circle(lon: float, lat: float, radius_m: float, quad_segs: int = 16) -> Polygon:
    """Círculo de ``radius_m`` metros reales alrededor de un punto WGS84."""
    if radius_m <= 0:
        raise ValueError("radius_m debe ser positivo")
    return to_wgs84(to_metric(Point(lon, lat)).buffer(radius_m, quad_segs=quad_segs))


def corridor(coords: Sequence[tuple[float, float]], half_width_m: float) -> Polygon:
    """Corredor vial: buffer plano de ``half_width_m`` a cada lado de una polilínea WGS84."""
    if half_width_m <= 0:
        raise ValueError("half_width_m debe ser positivo")
    line_m = to_metric(LineString(coords))
    return to_wgs84(line_m.buffer(half_width_m, cap_style="flat"))
