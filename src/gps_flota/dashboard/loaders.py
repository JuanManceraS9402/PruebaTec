"""Acceso a datos del tablero con caché de Streamlit."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
import psycopg
import streamlit as st

from gps_flota.config import DbRole, get_settings
from gps_flota.dashboard import data
from gps_flota.dashboard.basemap import POSITRON_STYLE_URL, hybrid_style
from gps_flota.routing import Place, RoutePlan, plan_route
from gps_flota.seed import bogota
from gps_flota.spatial.geofencing import GeofenceIndex

CACHE_TTL_S = 30
LOCAL_TZ = "America/Bogota"
HYBRID_STYLE_FILE = "ortofoto_hibrida.json"  # se genera al arrancar (no se versiona)
ORTHO_ONLY_STYLE_FILE = "ortofoto_bogota_2025.json"  # respaldo sin internet


@st.cache_resource
def connection(role: DbRole) -> psycopg.Connection:
    return psycopg.connect(get_settings().conninfo(role), autocommit=True)


def conn_for(role: DbRole) -> psycopg.Connection:
    """Conexión cacheada, verificada antes de usarla: si la base se reinició, se reabre."""
    conn = connection(role)
    try:
        conn.execute("SELECT 1")
    except psycopg.Error:
        connection.clear()
        conn = connection(role)
    return conn


@st.cache_resource(show_spinner=False)
def ortho_style_url(static_dir: Path) -> str:
    """Genera una vez el estilo híbrido (ortofoto + vías y nombres de CARTO). Sin
    internet, o si CARTO cambia su estilo, queda la ortofoto sola."""
    try:
        base = httpx.get(POSITRON_STYLE_URL, timeout=5).raise_for_status().json()
        style = hybrid_style(base)
        (static_dir / HYBRID_STYLE_FILE).write_text(json.dumps(style), encoding="utf-8")
    except (httpx.HTTPError, ValueError, KeyError, OSError):
        return f"/app/static/{ORTHO_ONLY_STYLE_FILE}"
    return f"/app/static/{HYBRID_STYLE_FILE}"


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


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_profile(role: DbRole, day: date, vehicle_key: str) -> pd.DataFrame:
    profile = data.vehicle_profile(conn_for(role), day, vehicle_key)
    if len(profile):
        profile["event_ts"] = pd.to_datetime(profile["event_ts"], utc=True).dt.tz_convert(LOCAL_TZ)
    return profile


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_sandbox_keys(role: DbRole) -> set[str]:
    return data.sandbox_vehicle_keys(conn_for(role))


@st.cache_resource(show_spinner=False)
def sandbox_catalog() -> tuple[list, GeofenceIndex]:
    return bogota.routes(), GeofenceIndex(bogota.geofences())


@st.cache_data(ttl=300, show_spinner=False)
def load_static(role: DbRole) -> tuple[pd.DataFrame, pd.DataFrame, tuple[str, bool]]:
    conn = conn_for(role)
    return data.geofences(conn), data.infraction_hotspots(conn), data.whoami(conn)


@st.cache_data(ttl=3600, show_spinner=False)
def load_route_plan(origin: Place, destination: Place) -> RoutePlan:
    return plan_route(origin, destination)
