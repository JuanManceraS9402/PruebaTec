"""Tablero BI: rutas, infracciones y Driver Score de la flota.

streamlit run dashboard/app.py

Se conecta con el usuario de base de datos del rol elegido en la barra lateral
(analista o administrador). El enmascaramiento de IDs lo hace PostgreSQL en las
vistas ``analytics.*``; el tablero solo muestra lo que la base le devuelve.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import streamlit as st

from gps_flota.dashboard.layers import SANDBOX_COLOR, TRACK_COLORS
from gps_flota.dashboard.loaders import load_day, load_days, load_sandbox_keys, load_static
from gps_flota.dashboard.sections import (
    render_analysis,
    render_credits,
    render_kpis,
    render_map,
    render_tables,
    resolve_focus,
)
from gps_flota.dashboard.sidebar import sidebar, vehicle_picker
from gps_flota.dashboard.simulator_ui import (
    MAP_ROUTE_MODE,
    render_sandbox_result,
    route_picker,
    simulator_panel,
)
from gps_flota.dashboard.views import vehicle_label

STATIC_DIR = Path(__file__).parent / "static"  # ver .streamlit/config.toml

st.set_page_config(page_title="Seguimiento GPS flota vehicular", page_icon="🗺️", layout="wide")


def main() -> None:
    role, enabled, simulator_slot = sidebar()
    try:
        days = load_days(role)
        geofences, hotspots, (db_user, is_admin) = load_static(role)
    except psycopg.OperationalError as exc:
        st.error(
            "No hay conexión con PostGIS. Levanta la base (`docker compose up -d --wait`, o "
            "sin Docker `python -m gps_flota.bootstrap`) y procesa datos con "
            f"`python -m gps_flota.processing`.\n\n`{exc}`"
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
    # El color sigue al vehículo (orden estable por clave), no a su posición en el
    # ranking: filtrar vehículos no repinta a los demás, y mapa y gráficos coinciden.
    simulated = load_sandbox_keys(role)
    if is_admin:
        with simulator_slot:
            simulator_panel(role, day, len(simulated))
    track_color = {
        key: TRACK_COLORS[i % len(TRACK_COLORS)]
        for i, key in enumerate(sorted(set(kpis["vehicle_key"]) - simulated))
    } | {key: SANDBOX_COLOR for key in simulated}
    kpis = kpis.assign(
        label=[
            vehicle_label(r.vehicle_id, r.vehicle_key)
            + (" · simulado" if r.vehicle_key in simulated else "")
            for r in kpis.itertuples()
        ]
    )
    render_sandbox_result(kpis, alerts)
    if is_admin and st.session_state.get("sandbox_route_mode") == MAP_ROUTE_MODE:
        route_picker()
    keys = vehicle_picker(kpis, simulated)
    kpis = kpis[kpis["vehicle_key"].isin(keys)]
    alerts = alerts[alerts["vehicle_key"].isin(keys)]

    render_kpis(kpis, alerts)
    if kpis.empty:
        st.info("Elige al menos un vehículo en la barra lateral.")
        st.stop()

    focus, picked = resolve_focus(kpis, keys)
    render_map(enabled, geofences, hotspots, kpis, alerts, track_color, focus, picked, STATIC_DIR)
    render_analysis(role, day, kpis, alerts, focus, track_color)
    render_tables(kpis, alerts)


main()
render_credits()
