"""Transformaciones puras de datos a capas de mapa e indicadores (sin E/S, sin Streamlit)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import pandas as pd

RGBA = list[int]

SEVERITY_COLORS: Final[Mapping[str, RGBA]] = {
    "LOW": [255, 214, 10, 200],
    "MEDIUM": [255, 140, 0, 210],
    "HIGH": [230, 57, 70, 220],
    "CRITICAL": [128, 0, 64, 230],
}
ZONE_COLORS: Final[Mapping[str, RGBA]] = {
    "URBAN": [120, 144, 156, 25],
    "HIGHWAY": [33, 150, 243, 45],
    "HIGH_RISK": [244, 67, 54, 60],
    "SCHOOL": [255, 193, 7, 80],
    "DEPOT": [76, 175, 80, 70],
    "RESTRICTED": [156, 39, 176, 70],
}
_FALLBACK: Final[RGBA] = [150, 150, 150, 120]

# Centro de Bogotá: vista inicial cuando no hay datos que encuadrar
BOGOTA_VIEW: Final = {"longitude": -74.10, "latitude": 4.65, "zoom": 11}


@dataclass(frozen=True, slots=True)
class FleetSummary:
    vehicles: int
    distance_km: float
    avg_driver_score: float | None
    worst_vehicle: str | None
    speeding_alerts: int
    fuel_alerts: int


def vehicle_label(vehicle_id: str, vehicle_key: str | None) -> str:
    """Etiqueta única: el ID enmascarado puede repetirse (VHC-****-ABC), el seudónimo no."""
    return f"{vehicle_id} · {vehicle_key}" if vehicle_key else vehicle_id


def line_path(geojson: str | None) -> list[list[float]]:
    """Coordenadas [lon, lat] de un LineString GeoJSON (vacío si no hay traza)."""
    if not geojson:
        return []
    geometry = json.loads(geojson)
    if geometry.get("type") != "LineString":
        raise ValueError(f"Se esperaba LineString, llegó {geometry.get('type')}")
    return [[float(x), float(y)] for x, y, *_ in geometry["coordinates"]]


def feature_collection(
    rows: Iterable[Mapping[str, Any]], geometry_field: str, properties: Iterable[str]
) -> dict[str, Any]:
    """FeatureCollection GeoJSON a partir de filas con la geometría serializada."""
    props = tuple(properties)
    features = [
        {
            "type": "Feature",
            "geometry": json.loads(row[geometry_field]),
            "properties": {p: _plain(row[p]) for p in props},
        }
        for row in rows
        if row.get(geometry_field)
    ]
    return {"type": "FeatureCollection", "features": features}


def _plain(value: Any) -> Any:
    """Valores serializables para el mapa (Decimal, numpy, NaN → nativos)."""
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item"):  # escalares numpy
        return _plain(value.item())
    return str(value)


def with_colors(frame: pd.DataFrame, column: str, palette: Mapping[str, RGBA]) -> pd.DataFrame:
    out = frame.copy()
    out["color"] = [list(palette.get(v, _FALLBACK)) for v in out[column]]
    return out


def fleet_summary(kpis: pd.DataFrame, alerts: pd.DataFrame) -> FleetSummary:
    scores = pd.to_numeric(kpis.get("driver_score"), errors="coerce") if len(kpis) else None
    worst = None
    if scores is not None and scores.notna().any():
        row = kpis.loc[scores.idxmin()]
        worst = vehicle_label(row["vehicle_id"], row.get("vehicle_key"))
    alert_types = alerts["alert_type"] if len(alerts) else pd.Series(dtype=str)
    return FleetSummary(
        vehicles=len(kpis),
        distance_km=float(pd.to_numeric(kpis["distance_km"]).sum()) if len(kpis) else 0.0,
        avg_driver_score=float(scores.mean())
        if scores is not None and scores.notna().any()
        else None,
        worst_vehicle=worst,
        speeding_alerts=int((alert_types == "SPEEDING").sum()),
        fuel_alerts=int((alert_types == "LOW_FUEL_RANGE").sum()),
    )


def view_for(paths: Iterable[list[list[float]]], points: pd.DataFrame) -> dict[str, float]:
    """Centro del encuadre: promedio de las coordenadas visibles."""
    lons: list[float] = []
    lats: list[float] = []
    for path in paths:
        lons.extend(p[0] for p in path)
        lats.extend(p[1] for p in path)
    if len(points):
        lons.extend(points["lon"].astype(float))
        lats.extend(points["lat"].astype(float))
    if not lons:
        return dict(BOGOTA_VIEW)
    return {
        "longitude": sum(lons) / len(lons),
        "latitude": sum(lats) / len(lats),
        "zoom": 12,
    }
