"""Simulación bajo demanda contra PostGIS: permisos, registro y limpieza."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString

from simon_geo.api.app import create_app
from simon_geo.config import DbRole
from simon_geo.sandbox import SandboxError, SandboxRequest, purge, run
from simon_geo.seed import bogota
from simon_geo.spatial.geofencing import GeofenceIndex

pytestmark = pytest.mark.integration

PLATE = "TST-9001-SBX"


@pytest.fixture
def admin(conn, seeded):
    c = conn(DbRole.ADMIN)
    c.autocommit = True
    c.execute("SELECT ops.sandbox_purge(%s)", (PLATE,))
    yield c
    c.execute("SELECT ops.sandbox_purge(%s)", (PLATE,))


def register(c: psycopg.Connection, plate: str = PLATE) -> None:
    c.execute("SELECT ops.sandbox_register(%s, 100, 20, 'R04')", (plate,))


def test_register_marks_vehicle_and_assigns_route(admin) -> None:
    register(admin)
    row = admin.execute(
        """
        SELECT v.vehicle_type, r.code
        FROM ref.vehicle v
        JOIN ref.vehicle_route_assignment a USING (vehicle_id)
        JOIN ref.route r USING (route_id)
        WHERE v.vehicle_id = %s AND a.valid_to IS NULL
        """,
        (PLATE,),
    ).fetchone()
    assert row == ("SANDBOX", "R04")


def test_duplicate_plate_is_rejected(admin) -> None:
    register(admin)
    with pytest.raises(psycopg.errors.UniqueViolation):
        register(admin)


def test_cannot_overwrite_or_purge_real_fleet(admin) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        register(admin, "VHC-8472-ABC")
    assert admin.execute("SELECT ops.sandbox_purge('VHC-8472-ABC')").fetchone()[0] == 0
    assert admin.execute(
        "SELECT vehicle_type FROM ref.vehicle WHERE vehicle_id = 'VHC-8472-ABC'"
    ).fetchone() == ("TRUCK",)


def test_purge_removes_every_layer(admin) -> None:
    register(admin)
    assert admin.execute("SELECT ops.sandbox_purge(%s)", (PLATE,)).fetchone()[0] == 1
    assert not admin.execute("SELECT 1 FROM ref.vehicle WHERE vehicle_id = %s", (PLATE,)).fetchone()


@pytest.mark.parametrize("role", [DbRole.ANALYST, DbRole.ETL, DbRole.INGEST])
def test_only_admin_can_use_the_simulator(conn, seeded, role) -> None:
    c = conn(role)
    c.autocommit = True
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        register(c)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("SELECT ops.sandbox_purge()")


def test_analyst_sees_only_pseudonyms(conn, admin) -> None:
    register(admin)
    analyst = conn(DbRole.ANALYST)
    keys = [r[0] for r in analyst.execute("SELECT vehicle_key FROM analytics.sandbox_vehicle")]
    assert keys and all(k.startswith("VK-") for k in keys)


def test_full_run_goes_through_every_layer_and_purges(admin, settings) -> None:
    """Formulario → API → Bronce → worker → Plata → Oro, y limpieza completa."""
    request = SandboxRequest(
        vehicle_id=PLATE,
        route_code="R05",
        profile="aggressive",
        fuel_pct=70.0,
        tank_l=120.0,
        consumption_l_100km=24.0,
        start=datetime.now(UTC) - timedelta(days=1),
    )
    routes, index = bogota.routes(), GeofenceIndex(bogota.geofences())
    key = settings.api_key_ingest.get_secret_value()
    with TestClient(create_app(settings=settings), headers={"X-API-Key": key}) as client:
        report = run(request, routes, index, settings, client=client)
        assert report.inserted > 0 and report.alerts_written > 0
        count = "SELECT count(*) FROM {} WHERE vehicle_id = %s"
        for table in ("bronze.telemetry_raw", "silver.telemetry_enriched", "silver.alert"):
            assert admin.execute(count.format(table), (PLATE,)).fetchone()[0] > 0, table
        assert admin.execute(count.format("gold.vehicle_daily_kpi"), (PLATE,)).fetchone()[0] > 0

        with pytest.raises(SandboxError, match="ya existe"):
            run(request, routes, index, settings, client=client)

    assert purge(settings, PLATE) == 1
    for table in (
        "bronze.telemetry_raw",
        "silver.telemetry_enriched",
        "silver.alert",
        "gold.vehicle_daily_kpi",
    ):
        assert admin.execute(count.format(table), (PLATE,)).fetchone()[0] == 0, table


def test_custom_route_is_atomic_and_purged(admin) -> None:
    wkb = LineString([(-74.15, 4.668), (-74.12, 4.68)]).wkb
    code = f"SBX-{PLATE}"
    call = "SELECT ops.sandbox_register_with_route(%s, 100, 20, %s, 'Prueba', %s)"
    admin.execute(call, (PLATE, code, wkb))
    has_route = "SELECT count(*) FROM ref.route WHERE code = %s"
    assert admin.execute(has_route, (code,)).fetchone()[0] == 1

    # placa de la flota real: se rechaza y su ruta no queda
    real = "VHC-8472-ABC"
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute(call, (real, f"SBX-{real}", wkb))
    assert admin.execute(has_route, (f"SBX-{real}",)).fetchone()[0] == 0

    # no se puede usar para escribir sobre una ruta de la flota
    with pytest.raises(psycopg.errors.RaiseException):
        admin.execute(call, ("TST-9002-SBX", "R01", wkb))

    assert admin.execute("SELECT ops.sandbox_purge(%s)", (PLATE,)).fetchone()[0] == 1
    assert admin.execute(has_route, (code,)).fetchone()[0] == 0
