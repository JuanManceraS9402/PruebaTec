"""Fixtures de integración: requieren `docker compose up -d`.

Sin base de datos los tests se SALTAN, salvo con REQUIRE_DB=1 (CI), donde fallan.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from simon_geo.config import DbRole, Settings
from simon_geo.db import connect
from simon_geo.reference import upsert_reference
from simon_geo.seed import bogota


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def db_available(settings: Settings) -> None:
    try:
        with connect(DbRole.OWNER, settings):
            pass
    except psycopg.OperationalError as exc:
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(f"REQUIRE_DB=1 pero PostGIS no responde: {exc}")
        pytest.skip("PostGIS no disponible (docker compose up -d)")


@pytest.fixture
def conn(
    settings: Settings, db_available: None
) -> Iterator[Callable[[DbRole], psycopg.Connection]]:
    """Fábrica de conexiones autocommit por rol; se cierran al final del test."""
    opened: list[psycopg.Connection] = []

    def _open(role: DbRole) -> psycopg.Connection:
        c = connect(role, settings, autocommit=True)
        opened.append(c)
        return c

    yield _open
    for c in opened:
        c.close()


@pytest.fixture(scope="session")
def seeded(settings: Settings, db_available: None) -> dict[str, int]:
    """Carga el escenario Bogotá. Devuelve {código de geocerca: id en BD}."""
    assignments = {
        sv.vehicle.vehicle_id: sv.route.code for sv in bogota.scenario(datetime.now(UTC))
    }
    with connect(DbRole.OWNER, settings) as c:
        return upsert_reference(
            c,
            vehicles=bogota.vehicles(),
            geofences=bogota.geofences(),
            routes=bogota.routes(),
            assignments=assignments,
        )
