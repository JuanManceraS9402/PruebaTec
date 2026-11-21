"""Tablero BI: rutas, infracciones y Driver Score de la flota.

streamlit run dashboard/app.py

Se conecta con el usuario de base de datos del rol elegido en la barra lateral
(analista o administrador). El enmascaramiento de IDs lo hace PostgreSQL en las
vistas ``analytics.*``; el tablero solo muestra lo que la base le devuelve.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import psycopg
import pydeck as pdk
import streamlit as st

from simon_geo.config import DbRole, get_settings
from simon_geo.dashboard import data
from simon_geo.dashboard.views import (
    SEVERITY_COLORS,
    ZONE_COLORS,
    feature_collection,
    fleet_summary,
    line_path,
    vehicle_label,
    view_for,
    with_colors,
)

ROLES = {"Analista BI (IDs enmascarados)": DbRole.ANALYST, "Administrador": DbRole.ADMIN}
TRACK_COLORS = [
    [0, 114, 178],
    [213, 94, 0],
    [0, 158, 115],
    [204, 121, 167],
    [86, 180, 233],
    [230, 159, 0],
]
SEVERITY_WEIGHT = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
CACHE_TTL_S = 30
LOCAL_TZ = "America/Bogota"

st.set_page_config(page_title="Simón Geo · Flota", page_icon="🗺️", layout="wide")


# ------------------------------------------------------------------- datos
@st.cache_resource
def connection(role: DbRole) -> psycopg.Connection:
    return psycopg.connect(get_settings().conninfo(role), autocommit=True)


def conn_for(role: DbRole) -> psycopg.Connection:
    conn = connection(role)
    if conn.closed or conn.broken:
        connection.clear()
        conn = connection(role)
    return conn


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_days(role: DbRole) -> list[date]:
    return data.available_days(conn_for(role))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_day(role: DbRole, day: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = conn_for(role)
    alerts = data.infractions(conn, day)
    if len(alerts):
        alerts["event_ts"] = pd.to_datetime(alerts["event_ts"], utc=True).dt.tz_convert(LOCAL_TZ)
    return data.daily_kpis(conn, day), alerts


@st.cache_data(ttl=300, show_spinner=False)
def load_static(role: DbRole) -> tuple[pd.DataFrame, pd.DataFrame, tuple[str, bool]]:
    conn = conn_for(role)
    return data.geofences(conn), data.infraction_grid(conn), data.whoami(conn)


# ------------------------------------------------------------------- capas
def geofence_layer(geofences: pd.DataFrame) -> pdk.Layer:
    fc = feature_collection(
        geofences.to_dict("records"), "geojson", ("code", "name", "zone_type", "speed_limit_kmh")
    )
    for f in fc["features"]:
        p = f["properties"]
        p["fill"] = list(ZONE_COLORS.get(p["zone_type"], [150, 150, 150, 40]))
        limit = f"{p['speed_limit_kmh']} km/h" if p["speed_limit_kmh"] else "sin límite"
        f["tooltip"] = f"<b>{p['name']}</b><br>{p['zone_type']} · {limit}"
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
        width_min_pixels=3,
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
    )


def alert_layer(alerts: pd.DataFrame) -> pdk.Layer:
    colored = with_colors(alerts, "severity", SEVERITY_COLORS)
    colored["tooltip"] = [
        f"<b>{r.alert_type} · {r.severity}</b><br>{vehicle_label(r.vehicle_id, r.vehicle_key)}"
        f"<br>{r.event_ts:%H:%M:%S}<br>{r.geofence_name or 'Fuera de geocerca'}"
        f"<br>observado {r.observed_value} / umbral {r.threshold_value}"
        for r in colored.itertuples()
    ]
    colored["event_ts"] = colored["event_ts"].astype(str)
    return pdk.Layer(
        "ScatterplotLayer",
        colored[["lon", "lat", "color", "tooltip"]],
        id="alerts",
        get_position=["lon", "lat"],
        get_fill_color="color",
        get_radius=45,
        radius_min_pixels=4,
        stroked=True,
        get_line_color=[255, 255, 255],
        pickable=True,
    )


def grid_layer(grid: pd.DataFrame) -> pdk.Layer:
    totals = (
        grid.groupby("cell_geojson", as_index=False)[["infractions", "severe_infractions"]]
        .sum()
        .to_dict("records")
    )
    fc = feature_collection(totals, "cell_geojson", ("infractions", "severe_infractions"))
    peak = max((f["properties"]["infractions"] for f in fc["features"]), default=1)
    for f in fc["features"]:
        p = f["properties"]
        alpha = int(60 + 170 * p["infractions"] / peak)
        p["fill"] = [230, 57, 70, alpha]
        f["tooltip"] = (
            f"<b>Celda 250 m</b><br>{p['infractions']} infracciones "
            f"({p['severe_infractions']} graves)"
        )
    return pdk.Layer(
        "GeoJsonLayer",
        fc,
        id="grid",
        filled=True,
        stroked=False,
        get_fill_color="properties.fill",
        pickable=True,
    )


# --------------------------------------------------------------------- UI
def sidebar() -> tuple[DbRole, dict[str, bool]]:
    st.sidebar.title("Simón Geo")
    role = ROLES[st.sidebar.radio("Ver como", list(ROLES))]
    st.sidebar.caption(
        "El mismo tablero, dos usuarios de base de datos. El enmascaramiento ocurre en "
        "PostgreSQL (vistas `analytics.*`), no aquí."
    )
    st.sidebar.subheader("Capas")
    layers = {
        "geofences": st.sidebar.checkbox("Geocercas", value=True),
        "tracks": st.sidebar.checkbox("Rutas del día", value=True),
        "heatmap": st.sidebar.checkbox("Mapa de calor de infracciones", value=True),
        "alerts": st.sidebar.checkbox("Puntos de infracción", value=True),
        "grid": st.sidebar.checkbox("Puntos calientes (malla 250 m, histórico)", value=False),
    }
    return role, layers


def render_kpis(kpis: pd.DataFrame, alerts: pd.DataFrame) -> None:
    s = fleet_summary(kpis, alerts)
    cols = st.columns(5)
    cols[0].metric("Vehículos", s.vehicles)
    cols[1].metric("Distancia", f"{s.distance_km:,.1f} km")
    cols[2].metric("Driver Score medio", f"{s.avg_driver_score:.1f}" if s.avg_driver_score else "—")
    cols[3].metric("Alertas de velocidad", s.speeding_alerts)
    cols[4].metric("Alertas de combustible", s.fuel_alerts)
    if s.worst_vehicle:
        st.caption(f"Menor Driver Score del día: **{s.worst_vehicle}**")


def main() -> None:
    role, enabled = sidebar()
    try:
        days = load_days(role)
        geofences, grid, (db_user, is_admin) = load_static(role)
    except psycopg.OperationalError as exc:
        st.error(
            "No hay conexión con PostGIS. Levanta la base con `docker compose up -d --wait` "
            f"y procesa datos con `python -m simon_geo.processing`.\n\n`{exc}`"
        )
        st.stop()

    st.title("Flota · rutas, infracciones y Driver Score")
    st.caption(
        f"Conectado como `{db_user}` · "
        + ("IDs reales (administrador)" if is_admin else "IDs enmascarados (analista)")
    )
    if not days:
        st.info("Aún no hay datos en la capa Oro. Ejecuta el simulador y el worker (SETUP.md).")
        st.stop()

    day = st.sidebar.selectbox("Día (hora de Bogotá)", days, format_func=lambda d: f"{d:%Y-%m-%d}")
    kpis, alerts = load_day(role, day)
    labels = {vehicle_label(r.vehicle_id, r.vehicle_key): r.vehicle_key for r in kpis.itertuples()}
    chosen = st.sidebar.multiselect("Vehículos", list(labels), default=list(labels))
    keys = {labels[c] for c in chosen}
    kpis = kpis[kpis["vehicle_key"].isin(keys)]
    alerts = alerts[alerts["vehicle_key"].isin(keys)]

    render_kpis(kpis, alerts)

    tracks = [
        {
            "name": vehicle_label(r.vehicle_id, r.vehicle_key),
            "path": line_path(r.track_geojson),
            "color": TRACK_COLORS[i % len(TRACK_COLORS)],
            "tooltip": f"<b>{vehicle_label(r.vehicle_id, r.vehicle_key)}</b>"
            f"<br>{r.distance_km} km · score {r.driver_score}",
        }
        for i, r in enumerate(kpis.itertuples())
        if r.track_geojson
    ]
    layers = []
    if enabled["geofences"] and len(geofences):
        layers.append(geofence_layer(geofences))
    if enabled["grid"] and len(grid):
        layers.append(grid_layer(grid))
    if enabled["heatmap"] and len(alerts):
        layers.append(heatmap_layer(alerts))
    if enabled["tracks"] and tracks:
        layers.append(track_layer(tracks))
    if enabled["alerts"] and len(alerts):
        layers.append(alert_layer(alerts))

    view = view_for((t["path"] for t in tracks), alerts)
    st.pydeck_chart(
        pdk.Deck(
            layers=layers,
            initial_view_state=pdk.ViewState(**view),
            tooltip={"html": "{tooltip}"},
            map_provider="carto",
            map_style="light",
        ),
        height=560,
    )

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Driver Score")
        st.dataframe(
            kpis.drop(columns=["track_geojson"]),
            hide_index=True,
            column_config={
                "driver_score": st.column_config.ProgressColumn(
                    "Driver Score", min_value=0, max_value=100, format="%.1f"
                ),
                "speeding_time_pct": st.column_config.NumberColumn("% tiempo en exceso"),
            },
        )
    with right:
        st.subheader("Infracciones del día")
        st.dataframe(
            alerts[
                [
                    "event_ts",
                    "vehicle_id",
                    "alert_type",
                    "severity",
                    "geofence_name",
                    "observed_value",
                    "threshold_value",
                ]
            ],
            hide_index=True,
        )


main()
