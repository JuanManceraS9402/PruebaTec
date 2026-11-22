"""Secciones de la página: KPIs, mapa, análisis, tablas y créditos."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

from simon_geo.config import DbRole
from simon_geo.dashboard import charts
from simon_geo.dashboard.layers import (
    TOOLTIP,
    alert_layer,
    geofence_layer,
    heatmap_layer,
    hotspot_layers,
    track_layer,
)
from simon_geo.dashboard.loaders import load_profile, ortho_style_url
from simon_geo.dashboard.views import (
    build_legend,
    fleet_summary,
    geofences_on_paths,
    legend_html,
    line_path,
    view_for,
)
from simon_geo.parameters import PipelineParameters

MAP_CSS = """
<style>
/* Interruptor de mapa base flotando sobre la esquina superior izquierda del mapa */
[data-testid="stColumn"]:has(.st-key-basemap_switch) { position: relative; }
.st-key-basemap_switch {
  position: absolute; top: 10px; left: 10px; z-index: 5; width: auto !important;
  background: rgba(24, 26, 31, 0.82); border-radius: 8px; padding: 4px 12px 2px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
}
.st-key-basemap_switch p { color: #fff !important; font-weight: 600; }
</style>
"""


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


def resolve_focus(kpis: pd.DataFrame, keys: set[str]) -> tuple[str, bool]:
    """Vehículo enfocado y si fue elegido: clic en el ranking (su estado ya está disponible
    al principio de la recarga), el último simulado o, si no hay, el de peor score."""
    picked = [
        p["vehicle_key"]
        for p in st.session_state.get("ranking", {}).get("selection", {}).get(charts.SELECTION, [])
        if p.get("vehicle_key") in keys
    ]
    last_simulated = st.session_state.get("sandbox_last", {}).get("key")
    if not picked and last_simulated in keys:
        picked = [last_simulated]
    focus = picked[0] if picked else kpis["vehicle_key"].iloc[0]
    return focus, bool(picked)


def render_map(
    enabled: dict[str, bool],
    geofences: pd.DataFrame,
    hotspots: pd.DataFrame,
    kpis: pd.DataFrame,
    alerts: pd.DataFrame,
    track_color: dict[str, list[int]],
    focus: str,
    picked: bool,
    static_dir: Path,
) -> None:
    # O se resalta el vehículo enfocado, o se deja solo él
    solo = enabled["only_focus"]
    map_kpis = kpis[kpis["vehicle_key"] == focus] if solo else kpis
    map_alerts = alerts[alerts["vehicle_key"] == focus] if solo else alerts
    highlight = picked and not solo
    tracks = [
        {
            "name": r.label,
            "path": line_path(r.track_geojson),
            "color": [
                *track_color[r.vehicle_key],
                255 if not highlight or r.vehicle_key == focus else 70,
            ],
            "line_px": 6 if highlight and r.vehicle_key == focus else 4,
            "title": r.label,
            "body": f"{r.distance_km} km recorridos · Driver Score {r.driver_score}",
        }
        for r in map_kpis.itertuples()
        if r.track_geojson
    ]
    layers = []
    shown_geofences = (
        geofences_on_paths(geofences, (t["path"] for t in tracks))
        if enabled["schools_on_route"]
        else geofences
    )
    if enabled["geofences"] and len(shown_geofences):
        layers.append(geofence_layer(shown_geofences))
    if enabled["hotspots"] and len(hotspots):
        layers.extend(hotspot_layers(hotspots))
    if enabled["heatmap"] and len(map_alerts):
        layers.append(heatmap_layer(map_alerts))
    if enabled["tracks"] and tracks:
        layers.append(track_layer(tracks))
    if enabled["alerts"] and len(map_alerts):
        layers.append(alert_layer(map_alerts))

    view = view_for((t["path"] for t in tracks), map_alerts)
    map_col, legend_col = st.columns([4, 1])
    with map_col:
        st.html(MAP_CSS)
        with st.container(key="basemap_switch"):
            ortho = st.toggle(
                "Ortofoto 2025",
                help="Ortofoto urbana 2025 de Bogotá (Catastro / IDECA) bajo las vías y los "
                "nombres del mapa; se ve a partir de zoom 10.",
            )
        map_style = ortho_style_url(static_dir) if ortho else "light"
        st.pydeck_chart(
            pdk.Deck(
                layers=layers,
                initial_view_state=pdk.ViewState(**view),
                tooltip=TOOLTIP,
                map_provider="carto",
                map_style=map_style,
            ),
            height=560,
        )
    with legend_col:
        st.markdown("**Leyenda**")
        legend = build_legend(
            enabled,
            zones=zip(
                shown_geofences["zone_type"], shown_geofences["speed_limit_kmh"], strict=True
            ),
            tracks=((t["name"], t["color"][:3]) for t in tracks),
            severities=map_alerts["severity"],
        )
        st.html(legend_html(legend))


def render_analysis(
    role: DbRole,
    day: date,
    kpis: pd.DataFrame,
    alerts: pd.DataFrame,
    focus: str,
    track_color: dict[str, list[int]],
) -> None:
    hex_colors = {k: charts.hex_color(c) for k, c in track_color.items()}
    rank_col, profile_col = st.columns([2, 3], gap="large")
    with rank_col:
        st.subheader("Ranking de Driver Score")
        st.caption("Clic en una barra para ver su perfil y resaltar su ruta · doble clic: quitar.")
        st.altair_chart(
            charts.score_ranking(kpis, hex_colors),
            key="ranking",
            on_select="rerun",
            width="stretch",
        )
    with profile_col:
        focus_label = kpis.loc[kpis["vehicle_key"] == focus, "label"].iloc[0]
        st.subheader(f"Perfil del día · {focus_label}")
        st.caption(
            "Arrastra para desplazar, rueda del mouse para hacer zoom, doble clic para volver."
        )
        profile = load_profile(role, day, focus)
        if profile.empty:
            st.info("Sin telemetría enriquecida para este vehículo en el día.")
        else:
            st.altair_chart(
                charts.vehicle_profile(
                    profile, hex_colors[focus], PipelineParameters().fuel_safety_factor
                )
            )

    if len(alerts):
        zone_col, time_col = st.columns(2, gap="large")
        with zone_col:
            st.subheader("Infracciones por zona")
            zone_view = st.radio(
                "Agrupar infracciones",
                ["Por tipo de zona", f"Top {charts.TOP_ZONES} geocercas"],
                horizontal=True,
                label_visibility="collapsed",
                key="zone_view",
            )
            st.altair_chart(
                charts.infractions_by_zone_type(alerts)
                if zone_view == "Por tipo de zona"
                else charts.infractions_by_zone(alerts)
            )
        with time_col:
            st.subheader("Infracciones en el tiempo")
            st.altair_chart(charts.infractions_over_time(alerts))


def render_tables(kpis: pd.DataFrame, alerts: pd.DataFrame) -> None:
    with st.expander("Tablas de detalle"):
        st.markdown("**Driver Score por vehículo**")
        st.dataframe(
            kpis.drop(columns=["track_geojson", "label"]),
            hide_index=True,
            column_config={
                "driver_score": st.column_config.ProgressColumn(
                    "Driver Score", min_value=0, max_value=100, format="%.1f"
                ),
                "speeding_time_pct": st.column_config.NumberColumn("% tiempo en exceso"),
            },
        )
        st.markdown("**Infracciones del día**")
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


DATOS_ABIERTOS = "https://datosabiertos.bogota.gov.co/dataset"
# (capa, fuente, enlace, licencia)
CREDITS = (
    (
        "Rutas, corredores y mapa del selector de puntos",
        "© colaboradores de OpenStreetMap; rutas calculadas con OSRM",
        "https://www.openstreetmap.org/copyright",
        "ODbL",
    ),
    (
        "Perímetro urbano (POT)",
        "Secretaría Distrital de Planeación",
        f"{DATOS_ABIERTOS}/perimetro-urbano-bogota-d-c",
        "CC BY 4.0",
    ),
    (
        "Zonas escolares (100 m por sede)",
        "Colegios Bogotá D.C. 12/2025, Secretaría de Educación del Distrito",
        f"{DATOS_ABIERTOS}/colegios-bogota-d-c",
        "CC BY-SA 4.0",
    ),
    (
        "Tramos y puntos críticos de siniestralidad 2022–2025",
        "Oficina de Seguridad Vial, Secretaría Distrital de Movilidad",
        "https://sig.simur.gov.co/arcgis/rest/services/DatosAbiertos",
        "CC BY 4.0",
    ),
    (
        "Ortofoto urbana 2025",
        "Secretaría Distrital de Planeación, publicada por IDECA – UAECD",
        "https://serviciosgis.catastrobogota.gov.co/image/rest/services/"
        "imagenesfunciones/orthourbana2025funcion/ImageServer",
        "Servicio público de IDECA",
    ),
    ("Mapa base", "© CARTO, © OpenStreetMap", "https://carto.com/attributions", "—"),
)


def render_credits() -> None:
    st.divider()
    with st.expander("Fuentes de datos y licencias", icon=":material/info:"):
        rows = [
            f"| {layer} | [{source}]({url}) | {license_} |"
            for layer, source, url, license_ in CREDITS
        ]
        st.markdown("\n".join(["| Capa | Fuente | Licencia |", "|---|---|---|", *rows]))
        st.caption("La flota, la telemetría y el patio logístico son simulados.")
