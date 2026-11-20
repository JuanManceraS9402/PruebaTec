"""Acceso a datos de la API (puertos + adaptador PostgreSQL)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from simon_geo.config import DbRole
from simon_geo.db import PoolRegistry
from simon_geo.domain import TelemetryPoint


class TelemetryWriter(Protocol):
    def insert_raw(self, points: Sequence[TelemetryPoint], source: str) -> int:
        """Inserta en Bronce ignorando duplicados. Devuelve cuántas filas se insertaron."""
        ...


class AlertReader(Protocol):
    def recent_alerts(self, role: DbRole, since: datetime, limit: int) -> list[dict[str, Any]]:
        """Alertas desde ``since``, leídas con el rol de BD indicado (que decide el enmascarado)."""
        ...


_INSERT_RAW = """
INSERT INTO bronze.telemetry_raw
    (vehicle_id, event_ts, lat, lon, speed_kmh, fuel_level_pct, source)
SELECT u.vehicle_id, u.event_ts, u.lat, u.lon, u.speed_kmh, u.fuel_level_pct, %(source)s
FROM unnest(
    %(vehicle_id)s::text[],
    %(event_ts)s::timestamptz[],
    %(lat)s::float8[],
    %(lon)s::float8[],
    %(speed_kmh)s::real[],
    %(fuel_level_pct)s::real[]
) AS u(vehicle_id, event_ts, lat, lon, speed_kmh, fuel_level_pct)
ON CONFLICT (vehicle_id, event_ts) DO NOTHING
"""

_RECENT_ALERTS = """
SELECT alert_id, vehicle_id, vehicle_key, event_ts,
       alert_type::text AS alert_type, severity::text AS severity, geofence_id,
       ST_Y(geom) AS lat, ST_X(geom) AS lon,
       observed_value, threshold_value, details
FROM analytics.alert
WHERE event_ts >= %s
ORDER BY event_ts DESC
LIMIT %s
"""


class PostgresRepository:
    """Un solo INSERT ... SELECT FROM unnest(arrays) por lote: un viaje de red y
    un plan, en vez de N INSERTs. La PK convierte las retransmisiones en no-ops."""

    def __init__(self, pools: PoolRegistry) -> None:
        self._pools = pools
        self._known_days: set[date] = set()
        self._lock = threading.Lock()

    def insert_raw(self, points: Sequence[TelemetryPoint], source: str) -> int:
        if not points:
            return 0
        with self._pools.get(DbRole.INGEST).connection() as conn, conn.cursor() as cur:
            self._ensure_partitions(cur, points)
            cur.execute(
                _INSERT_RAW,
                {
                    "source": source,
                    "vehicle_id": [p.vehicle_id for p in points],
                    "event_ts": [p.event_ts for p in points],
                    "lat": [p.lat for p in points],
                    "lon": [p.lon for p in points],
                    "speed_kmh": [p.speed_kmh for p in points],
                    "fuel_level_pct": [p.fuel_level_pct for p in points],
                },
            )
            return cur.rowcount

    def _ensure_partitions(self, cur: psycopg.Cursor, points: Sequence[TelemetryPoint]) -> None:
        """Crea particiones diarias (UTC) que falten; cachea las ya verificadas."""
        utc_days = {p.event_ts.astimezone(UTC).date() for p in points}
        with self._lock:
            missing = utc_days - self._known_days
        if not missing:
            return
        cur.execute(
            "SELECT ops.ensure_daily_partitions('bronze.telemetry_raw', %s, %s)",
            (min(missing), max(missing)),
        )
        with self._lock:
            self._known_days |= missing

    def recent_alerts(self, role: DbRole, since: datetime, limit: int) -> list[dict[str, Any]]:
        if role not in (DbRole.ANALYST, DbRole.ADMIN):
            raise ValueError(f"El rol {role} no puede leer alertas")
        with (
            self._pools.get(role).connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(_RECENT_ALERTS, (since, limit))
            return cur.fetchall()
