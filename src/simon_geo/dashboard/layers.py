"""Capas pydeck del mapa principal (sin Streamlit)."""

from __future__ import annotations

import pandas as pd
import pydeck as pdk

from simon_geo.dashboard.views import (
    HEATMAP_COLORS,
    HOTSPOT_COLOR,
    SEVERITY_COLORS,
    ZONE_COLORS,
    ZONE_LABELS,
    feature_collection,
    vehicle_label,
    with_colors,
)

# Un color por vehículo (10). Okabe-Ito sin amarillo ni naranja (reservados a la
# severidad de las alertas), completado buscando la mayor distancia OKLab con visión
# normal, deuteranopía y protanopía: ΔE mínimo 7,6. Por debajo de 8 se exige una
# segunda codificación, y la hay: leyenda, tooltip con el nombre, resaltado por clic y
# "Solo el vehículo seleccionado". Luminosidad media: se ve sobre CARTO y la ortofoto.
TRACK_COLORS = [
    [0, 114, 178],  # #0072b2
    [86, 180, 233],  # #56b4e9
    [0, 158, 115],  # #009e73
    [204, 121, 167],  # #cc79a7
    [140, 86, 75],  # #8c564b
    [182, 232, 128],  # #b6e880
    [99, 110, 250],  # #636efa
    [44, 160, 44],  # #2ca02c
    [93, 109, 126],  # #5d6d7e
    [0, 204, 150],  # #00cc96
]
SEVERITY_WEIGHT = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
ALERT_LABELS = {"SPEEDING": "Exceso de velocidad", "LOW_FUEL_RANGE": "Combustible insuficiente"}
SEVERITY_SHORT = {"LOW": "baja", "MEDIUM": "media", "HIGH": "alta", "CRITICAL": "crítica"}
# La estructura va en la plantilla y los datos como texto plano: deck.gl escapa los
# valores que interpola, así que un <b> dentro del dato se vería literal.
TOOLTIP = {
    "html": "<b>{title}</b><div style='white-space:pre-line;margin-top:2px'>{body}</div>",
    "style": {"fontSize": "13px", "maxWidth": "320px"},
}
SANDBOX_COLOR = [255, 0, 200]  # los vehículos simulados no cambian el color de la flota


def geofence_layer(geofences: pd.DataFrame) -> pdk.Layer:
    fc = feature_collection(
        geofences.to_dict("records"), "geojson", ("code", "name", "zone_type", "speed_limit_kmh")
    )
    for f in fc["features"]:
        p = f["properties"]
        p["fill"] = list(ZONE_COLORS.get(p["zone_type"], [150, 150, 150, 40]))
        limit = f"{p['speed_limit_kmh']} km/h" if p["speed_limit_kmh"] else "sin límite"
        f["title"] = p["name"]
        f["body"] = f"{ZONE_LABELS.get(p['zone_type'], p['zone_type'])} · {limit}"
    return pdk.Layer(
        "GeoJsonLayer",
        fc,
        id="geofences",
        stroked=True,
        filled=True,
        get_fill_color="properties.fill",
        get_line_color=[60, 60, 60, 160],
        line_width_min_pixels=1,
        pickable=True,
    )


def track_layer(tracks: list[dict]) -> pdk.Layer:
    return pdk.Layer(
        "PathLayer",
        tracks,
        id="tracks",
        get_path="path",
        get_color="color",
        get_width="line_px",
        # pydeck convierte todo texto en expresión ("@@=pixels" → indefinido, y la ruta
        # se dibuja en unidades del mundo, gigante): hay que marcarlo como literal
        width_units=pdk.types.String("pixels"),
        joint_rounded=True,
        cap_rounded=True,
        pickable=True,
    )


def heatmap_layer(alerts: pd.DataFrame) -> pdk.Layer:
    weighted = alerts.assign(weight=alerts["severity"].map(SEVERITY_WEIGHT).fillna(1))
    return pdk.Layer(
        "HeatmapLayer",
        weighted[["lon", "lat", "weight"]],
        id="heatmap",
        get_position=["lon", "lat"],
        get_weight="weight",
        radius_pixels=40,
        opacity=0.6,
        color_range=[list(c) for c in HEATMAP_COLORS],
    )


def unit(alert_type: str) -> str:
    return " km/h" if alert_type == "SPEEDING" else ""


def alert_layer(alerts: pd.DataFrame) -> pdk.Layer:
    colored = with_colors(alerts, "severity", SEVERITY_COLORS)
    colored["title"] = [
        f"{ALERT_LABELS.get(r.alert_type, r.alert_type)} · "
        f"severidad {SEVERITY_SHORT.get(r.severity, r.severity)}"
        for r in colored.itertuples()
    ]
    colored["body"] = [
        "\n".join(
            (
                vehicle_label(r.vehicle_id, r.vehicle_key),
                f"{r.event_ts:%H:%M:%S} · {r.geofence_name or 'Fuera de geocerca'}",
                f"Observado {r.observed_value}{unit(r.alert_type)} · "
                f"umbral {r.threshold_value}{unit(r.alert_type)}",
            )
        )
        for r in colored.itertuples()
    ]
    colored["event_ts"] = colored["event_ts"].astype(str)
    return pdk.Layer(
        "ScatterplotLayer",
        colored[["lon", "lat", "color", "title", "body"]],
        id="alerts",
        get_position=["lon", "lat"],
        get_fill_color="color",
        get_radius=45,
        radius_min_pixels=4,
        stroked=True,
        get_line_color=[255, 255, 255],
        pickable=True,
    )


def hotspot_layers(hotspots: pd.DataFrame) -> list[pdk.Layer]:
    """Zonas calientes: polígono con opacidad según infracciones y su número de ranking."""
    records = hotspots.to_dict("records")
    fc = feature_collection(
        records,
        "geojson",
        ("hotspot_rank", "infractions", "severe_infractions", "vehicles", "area_km2"),
    )
    peak = max((r["infractions"] for r in records), default=1)
    for f in fc["features"]:
        p = f["properties"]
        p["fill"] = [*HOTSPOT_COLOR, int(40 + 110 * p["infractions"] / peak)]
        f["title"] = f"Zona caliente #{p['hotspot_rank']}"
        f["body"] = (
            f"{p['infractions']} excesos ({p['severe_infractions']} graves) · "
            f"{p['vehicles']} vehículo(s)\n{p['area_km2']} km² · histórico acumulado"
        )
    labels = [
        {"position": [r["label_lon"], r["label_lat"]], "text": f"#{r['hotspot_rank']}"}
        for r in records
    ]
    return [
        pdk.Layer(
            "GeoJsonLayer",
            fc,
            id="hotspots",
            filled=True,
            stroked=True,
            get_fill_color="properties.fill",
            get_line_color=[*HOTSPOT_COLOR, 230],
            line_width_min_pixels=2,
            pickable=True,
        ),
        pdk.Layer(
            "TextLayer",
            labels,
            id="hotspot-labels",
            get_position="position",
            get_text="text",
            get_size=15,
            get_color=[255, 255, 255],
            outline_width=3,
            outline_color=[*HOTSPOT_COLOR, 255],
            font_weight=700,
            font_settings={"sdf": True},
        ),
    ]
