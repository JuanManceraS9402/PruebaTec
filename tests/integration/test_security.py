"""Privacidad aplicada en la base de datos: roles y enmascaramiento."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from gps_flota.config import DbRole

pytestmark = pytest.mark.integration

VEHICLE = "VHC-8472-ABC"


@pytest.fixture
def event_ts(conn, seeded) -> datetime:
    """Inserta un paquete con timestamp único y lo devuelve."""
    ts = datetime.now(UTC) - timedelta(minutes=1)  # precisión de µs: único por ejecución
    c = conn(DbRole.INGEST)
    c.execute(
        "SELECT ops.ensure_daily_partitions('bronze.telemetry_raw', %s, %s)",
        (ts.date(), ts.date()),
    )
    c.execute(
        """
        INSERT INTO bronze.telemetry_raw (vehicle_id, event_ts, lat, lon, speed_kmh, fuel_level_pct)
        VALUES (%s, %s, 4.65, -74.08, 40, 50) ON CONFLICT DO NOTHING
        """,
        (VEHICLE, ts),
    )
    return ts


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM bronze.telemetry_raw LIMIT 1",
        "SELECT * FROM silver.telemetry_enriched LIMIT 1",
        "SELECT * FROM gold.vehicle_daily_kpi LIMIT 1",
        "SELECT * FROM ref.vehicle LIMIT 1",
        "SELECT * FROM security.vehicle_pseudonym LIMIT 1",
        "SELECT * FROM security.secret",
        "SELECT security.compute_vehicle_key('VHC-8472-ABC')",
    ],
)
def test_analyst_cannot_bypass_analytics(conn, query: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn(DbRole.ANALYST).execute(query)


def test_ingest_cannot_read_telemetry(conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn(DbRole.INGEST).execute("SELECT lat, lon FROM bronze.telemetry_raw LIMIT 1")


def test_analyst_sees_masked_id_and_pseudonym(conn, event_ts) -> None:
    vehicle_id, vehicle_key = (
        conn(DbRole.ANALYST)
        .execute(
            "SELECT vehicle_id, vehicle_key FROM analytics.telemetry_raw WHERE event_ts = %s",
            (event_ts,),
        )
        .fetchone()
    )
    assert vehicle_id == "VHC-****-ABC"
    assert vehicle_key.startswith("VK-") and len(vehicle_key) == 15


def test_admin_sees_real_id_with_same_view(conn, event_ts) -> None:
    rows = {
        role: conn(role)
        .execute(
            "SELECT vehicle_id, vehicle_key FROM analytics.telemetry_raw WHERE event_ts = %s",
            (event_ts,),
        )
        .fetchone()
        for role in (DbRole.ADMIN, DbRole.ANALYST)
    }
    assert rows[DbRole.ADMIN][0] == VEHICLE
    # el seudónimo es el mismo para ambos: permite cruzar reportes sin exponer la placa
    assert rows[DbRole.ADMIN][1] == rows[DbRole.ANALYST][1]


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("VHC-8472-ABC", "VHC-****-ABC"),
        ("TRK-0001-ZZZ", "TRK-****-ZZZ"),
        ("formato-raro", "***MASKED***"),
    ],
)
def test_mask_function(conn, raw: str, masked: str) -> None:
    assert (
        conn(DbRole.ANALYST).execute("SELECT security.mask_vehicle_id(%s)", (raw,)).fetchone()[0]
        == masked
    )
