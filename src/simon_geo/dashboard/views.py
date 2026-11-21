"""Transformaciones puras de datos a capas de mapa e indicadores (sin E/S, sin Streamlit)."""

from __future__ import annotations

import html
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import pandas as pd
import shapely
from shapely.geometry import LineString

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


def geofences_on_paths(
    geofences: pd.DataFrame,
    paths: Iterable[list[list[float]]],
    zone_types: frozenset[str] = frozenset({"SCHOOL"}),
) -> pd.DataFrame:
    """Deja de ``zone_types`` solo las geocercas que tocan alguna ruta; el resto pasa
    completo. Con ~2.200 zonas escolares reales, dibujarlas todas tapa el mapa.

    ``geofences`` necesita ``zone_type`` y ``geojson``. Usa un R-tree (STRtree) sobre
    las geometrías candidatas: una consulta por ruta en lugar de una por geocerca.
    """
    lines = [LineString(p) for p in paths if len(p) >= 2]
    filtered = geofences["zone_type"].isin(zone_types)
    if not filtered.any():
        return geofences
    candidates = geofences[filtered]
    keep = set()
    if lines:
        tree = shapely.STRtree(shapely.from_geojson(candidates["geojson"].to_numpy()))
        for line in lines:
            keep.update(tree.query(line, predicate="intersects").tolist())
    on_path = candidates.iloc[sorted(keep)]
    return pd.concat([geofences[~filtered], on_path]).sort_index()


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


# -------------------------------------------------------------------- leyenda
ZONE_LABELS: Final[Mapping[str, str]] = {
    "SCHOOL": "Zona escolar",
    "RESTRICTED": "Zona restringida",
    "HIGH_RISK": "Alta siniestralidad",
    "DEPOT": "Patio logístico",
    "HIGHWAY": "Corredor vial",
    "URBAN": "Perímetro urbano",
}
SEVERITY_LABELS: Final[Mapping[str, str]] = {
    "LOW": "Baja (exceso < 10 km/h)",
    "MEDIUM": "Media (10–20 km/h)",
    "HIGH": "Alta (20–30 km/h)",
    "CRITICAL": "Crítica (≥ 30 km/h o sin combustible para la ruta)",
}
# Rampa del mapa de calor (de menor a mayor concentración); se pasa también a la capa
HEATMAP_COLORS: Final[tuple[RGBA, ...]] = (
    [255, 255, 178],
    [254, 217, 118],
    [254, 178, 76],
    [253, 141, 60],
    [240, 59, 32],
    [189, 0, 38],
)
HOTSPOT_COLOR: Final[RGBA] = [189, 0, 38]


@dataclass(frozen=True, slots=True)
class LegendItem:
    label: str
    colors: tuple[RGBA, ...]  # uno, o varios para una rampa
    shape: str  # "area" | "line" | "dot" | "ramp"


@dataclass(frozen=True, slots=True)
class LegendSection:
    title: str
    items: tuple[LegendItem, ...]
    note: str = ""


def build_legend(
    enabled: Mapping[str, bool],
    *,
    zones: Iterable[tuple[str, int | None]] = (),
    tracks: Iterable[tuple[str, RGBA]] = (),
    severities: Iterable[str] = (),
) -> list[LegendSection]:
    """Secciones de leyenda solo para las capas encendidas, en el orden en que se apilan
    en el mapa (la de arriba primero). Solo lista los valores presentes en los datos."""
    sections: list[LegendSection] = []
    present = set(severities)
    if enabled.get("alerts") and present:
        sections.append(
            LegendSection(
                "Puntos de infracción",
                tuple(
                    LegendItem(SEVERITY_LABELS[s], (list(SEVERITY_COLORS[s]),), "dot")
                    for s in SEVERITY_COLORS
                    if s in present
                ),
                "Una alerta por episodio; color = severidad.",
            )
        )
    track_items = tuple(LegendItem(name, (list(color),), "line") for name, color in tracks)
    if enabled.get("tracks") and track_items:
        sections.append(LegendSection("Rutas del día", track_items))
    if enabled.get("heatmap") and present:
        sections.append(
            LegendSection(
                "Mapa de calor",
                (LegendItem("menos → más infracciones", HEATMAP_COLORS, "ramp"),),
                "Ponderado por severidad.",
            )
        )
    if enabled.get("hotspots"):
        sections.append(
            LegendSection(
                "Zonas calientes (histórico)",
                (
                    LegendItem(
                        "menos → más infracciones",
                        ([*HOTSPOT_COLOR, 40], [*HOTSPOT_COLOR, 150]),
                        "ramp",
                    ),
                ),
                "≥ 3 excesos a menos de 600 m (DBSCAN). #1 = la zona con más excesos.",
            )
        )
    limits: dict[str, set[int]] = {}
    for zone_type, limit in zones:
        limits.setdefault(zone_type, set())
        if limit is not None:
            limits[zone_type].add(int(limit))
    if enabled.get("geofences") and limits:
        items = []
        for zone_type in ZONE_LABELS:
            if zone_type not in limits:
                continue
            kmh = "/".join(str(v) for v in sorted(limits[zone_type]))
            label = ZONE_LABELS[zone_type] + (f" · {kmh} km/h" if kmh else "")
            r, g, b, a = ZONE_COLORS.get(zone_type, _FALLBACK)
            # en la leyenda el relleno se ve más opaco que en el mapa para que se distinga
            items.append(LegendItem(label, ([r, g, b, max(a, 110)],), "area"))
        sections.append(LegendSection("Geocercas", tuple(items), "Límite de velocidad aplicado."))
    return sections


def _css(color: RGBA) -> str:
    r, g, b, *rest = color
    alpha = rest[0] / 255 if rest else 1.0
    return f"rgba({r},{g},{b},{alpha:.2f})"


def _swatch(item: LegendItem) -> str:
    if item.shape == "ramp":
        stops = ",".join(_css(c) for c in item.colors)
        style = (
            f"width:56px;height:10px;border-radius:3px;background:linear-gradient(90deg,{stops})"
        )
    elif item.shape == "line":
        style = f"width:22px;height:0;border-top:4px solid {_css(item.colors[0])};border-radius:2px"
    elif item.shape == "dot":
        style = (
            f"width:11px;height:11px;border-radius:50%;background:{_css(item.colors[0])};"
            "border:1.5px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.25)"
        )
    else:  # area
        style = (
            f"width:16px;height:12px;background:{_css(item.colors[0])};"
            "border:1px solid rgba(60,60,60,.6)"
        )
    return f'<span style="display:inline-block;flex:none;{style}"></span>'


_NOTE_STYLE: Final = "opacity:.65;font-size:.78em;margin-top:2px"


def legend_html(sections: Iterable[LegendSection]) -> str:
    """Leyenda en HTML autocontenido (los textos se escapan)."""
    blocks = []
    for section in sections:
        rows = "".join(
            '<div style="display:flex;align-items:center;gap:8px;margin:3px 0">'
            f"{_swatch(item)}<span>{html.escape(item.label)}</span></div>"
            for item in section.items
        )
        note = (
            f'<div style="{_NOTE_STYLE}">{html.escape(section.note)}</div>' if section.note else ""
        )
        blocks.append(
            '<div style="margin-bottom:12px">'
            f'<div style="font-weight:600;margin-bottom:4px">{html.escape(section.title)}</div>'
            f"{rows}{note}</div>"
        )
    if not blocks:
        return '<div style="opacity:.6">No hay capas encendidas.</div>'
    return '<div style="font-size:.85em;line-height:1.25">' + "".join(blocks) + "</div>"
