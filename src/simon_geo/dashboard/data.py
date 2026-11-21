"""Consultas del tablero.

Solo leen el esquema ``analytics``: el tablero se conecta como analista (IDs
enmascarados) o como administrador, y es la base de datos la que decide qué ve
cada uno. No hay lógica de enmascaramiento en el tablero.
"""

from __future__ import annotations

from datetime import date
from typing import Final

import pandas as pd
import psycopg

_DAYS: Final = """
SELECT DISTINCT local_day FROM analytics.vehicle_daily_kpi ORDER BY local_day DESC
"""

_DAILY_KPI: Final = """
SELECT vehicle_id, vehicle_key, points, distance_km, avg_moving_speed_kmh, max_speed_kmh,
       speeding_time_pct, max_excess_kmh, harsh_events, refuels, fuel_used_l,
       consumption_l_100km, low_fuel_risk_points, driver_score,
       ST_AsGeoJSON(track_geom, 6) AS track_geojson
FROM analytics.vehicle_daily_kpi
WHERE local_day = %(day)s
ORDER BY driver_score ASC NULLS LAST, vehicle_key
"""

_INFRACTIONS: Final = """
SELECT alert_id, vehicle_id, vehicle_key, event_ts, alert_type::text AS alert_type,
       severity::text AS severity, geofence_name, zone_type::text AS zone_type,
       observed_value, threshold_value,
       ST_X(geom) AS lon, ST_Y(geom) AS lat
FROM analytics.infraction_point
WHERE local_day = %(day)s
ORDER BY event_ts
"""

_HOTSPOTS: Final = """
SELECT hotspot_rank, infractions, severe_infractions, vehicles, area_km2,
       ST_AsGeoJSON(geom, 6) AS geojson,
       ST_X(ST_PointOnSurface(geom)) AS label_lon, ST_Y(ST_PointOnSurface(geom)) AS label_lat
FROM analytics.infraction_hotspot
ORDER BY hotspot_rank
"""

_GEOFENCES: Final = """
SELECT code, name, zone_type::text AS zone_type, speed_limit_kmh,
       ST_AsGeoJSON(geom, 6) AS geojson
FROM analytics.geofence
ORDER BY geofence_id
"""

# Perfil de un vehículo en un día local; el rango sobre event_ts permite podar particiones
_PROFILE: Final = """
SELECT event_ts, speed_kmh, applied_speed_limit_kmh, speed_excess_kmh, is_speeding,
       fuel_level_pct, est_range_km, remaining_route_km, is_low_fuel_risk
FROM analytics.telemetry_enriched
WHERE vehicle_key = %(vehicle_key)s
  AND event_ts >= (%(day)s::date)::timestamp AT TIME ZONE 'America/Bogota'
  AND event_ts <  (%(day)s::date + 1)::timestamp AT TIME ZONE 'America/Bogota'
  AND NOT is_gps_outlier
ORDER BY event_ts
"""

_WHOAMI: Final = "SELECT current_user, security.is_admin()"


def _frame(conn: psycopg.Connection, query: str, params: dict | None = None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


def available_days(conn: psycopg.Connection) -> list[date]:
    return [row[0] for row in conn.execute(_DAYS).fetchall()]


def daily_kpis(conn: psycopg.Connection, day: date) -> pd.DataFrame:
    return _frame(conn, _DAILY_KPI, {"day": day})


def infractions(conn: psycopg.Connection, day: date) -> pd.DataFrame:
    return _frame(conn, _INFRACTIONS, {"day": day})


def infraction_hotspots(conn: psycopg.Connection) -> pd.DataFrame:
    """Zonas calientes históricas (DBSCAN en la capa Oro), de mayor a menor."""
    return _frame(conn, _HOTSPOTS)


def vehicle_profile(conn: psycopg.Connection, day: date, vehicle_key: str) -> pd.DataFrame:
    """Serie temporal (velocidad, límite, combustible) de un vehículo en un día local."""
    return _frame(conn, _PROFILE, {"day": day, "vehicle_key": vehicle_key})


def geofences(conn: psycopg.Connection) -> pd.DataFrame:
    return _frame(conn, _GEOFENCES)


def whoami(conn: psycopg.Connection) -> tuple[str, bool]:
    user, is_admin = conn.execute(_WHOAMI).fetchone()
    return user, bool(is_admin)
