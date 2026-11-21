"""Conexiones a PostgreSQL por rol."""

from __future__ import annotations

import threading

import psycopg
from psycopg_pool import ConnectionPool

from simon_geo.config import DbRole, Settings, get_settings


def connect(role: DbRole, settings: Settings | None = None, **kwargs) -> psycopg.Connection:
    """Conexión directa (scripts, seed, tests)."""
    return psycopg.connect((settings or get_settings()).conninfo(role), **kwargs)


class PoolRegistry:
    """Un pool por rol, creado al primer uso (la app arranca aunque la BD no esté lista)."""

    def __init__(self, settings: Settings, max_size: int = 10) -> None:
        self._settings = settings
        self._max_size = max_size
        self._pools: dict[DbRole, ConnectionPool] = {}
        self._lock = threading.Lock()

    def get(self, role: DbRole) -> ConnectionPool:
        with self._lock:
            pool = self._pools.get(role)
            if pool is None:
                pool = ConnectionPool(
                    self._settings.conninfo(role),
                    min_size=1,
                    max_size=self._max_size,
                    name=f"simon_geo_{role.value}",
                    # Verifica la conexión al entregarla: tras un reinicio de la base
                    # el pool descarta las conexiones muertas en vez de fallar.
                    check=ConnectionPool.check_connection,
                    open=True,
                )
                self._pools[role] = pool
            return pool

    def close(self) -> None:
        with self._lock:
            for pool in self._pools.values():
                pool.close()
            self._pools.clear()
