"""Worker Bronce → Plata → Oro contra PostGIS real.

Ingesta el escenario Bogotá, ejecuta el worker y comprueba que lo escrito en
la base coincide con el pipeline en memoria y con lo que cada vehículo del
escenario debe disparar.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from simon_geo.api.repository import PostgresRepository
from simon_geo.config import DbRole
from simon_geo.db import PoolRegistry, connect
from simon_geo.domain import TelemetryPoint
from simon_geo.parameters import PipelineParameters
from simon_geo.processing.enrichment import SilverEnricher
from simon_geo.processing.worker import BronzeToSilverWorker, WorkerConfig
from simon_geo.seed import bogota
from simon_geo.simulator.fleet import FleetSimulator
from simon_geo.spatial.geofencing import GeofenceIndex

pytestmark = pytest.mark.integration

# 06:00 en Bogotá de hace dos días: ventana propia, lejos de los datos de otros tests
START = (datetime.now(UTC) - timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
WINDOW = (START - timedelta(days=1), START + timedelta(days=1))
VEHICLE_IDS = [v.vehicle_id for v in bogota.vehicles()]
CONFIG = WorkerConfig(settle=timedelta(0))


def scenario_points() -> list[TelemetryPoint]:
    """Puntos simulados, sin retransmisiones (se conserva el primero, como la PK de Bronce)."""
    index = GeofenceIndex(bogota.geofences())
    unique: dict[tuple[str, datetime], TelemetryPoint] = {}
    for p in FleetSimulator(bogota.scenario(START), index).run():
        unique.setdefault((p.vehicle_id, p.event_ts), p)
    return list(unique.values())


def alerts_in_window(conn: psycopg.Connection) -> set[tuple]:
    rows = conn.execute(
        """
        SELECT a.vehicle_id, a.event_ts, a.alert_type::text, a.severity::text, g.code
        FROM silver.alert a
        LEFT JOIN ref.geofence g USING (geofence_id)
        WHERE a.vehicle_id = ANY(%s) AND a.event_ts >= %s AND a.event_ts < %s
        """,
        (VEHICLE_IDS, *WINDOW),
    ).fetchall()
    return set(rows)


@pytest.fixture(scope="module")
def etl_conn(settings, db_available) -> Iterator[psycopg.Connection]:
    with connect(DbRole.ETL, settings) as c:
        yield c


@pytest.fixture(scope="module")
def processed(settings, db_available, seeded, etl_conn) -> list[TelemetryPoint]:
    """Escenario ingestado y procesado una vez. Devuelve los puntos ingestados."""
    worker = BronzeToSilverWorker(etl_conn, CONFIG)
    worker.run_once()  # drena lo que hayan dejado otros tests

    # Ventana limpia (re-ejecuciones locales sobre la misma base)
    with connect(DbRole.OWNER, settings) as owner:
        for table in ("silver.alert", "silver.telemetry_enriched", "bronze.telemetry_raw"):
            owner.execute(
                f"DELETE FROM {table} "
                "WHERE vehicle_id = ANY(%s) AND event_ts >= %s AND event_ts < %s",
                (VEHICLE_IDS, *WINDOW),
            )

    points = scenario_points()
    pools = PoolRegistry(settings, max_size=1)
    try:
        PostgresRepository(pools).insert_raw(points, source="test")
    finally:
        pools.close()

    report = worker.run_once()
    assert report.vehicles == len(VEHICLE_IDS)
    assert report.new_rows == len(points)
    assert report.gold_refreshed
    return points


def test_alerts_match_in_memory_pipeline(processed, conn) -> None:
    """Mismas alertas que el pipeline en memoria: geocercas de PostGIS, lookback
    y escritura no alteran el resultado."""
    geofences = bogota.geofences()
    code_by_id = {g.geofence_id: g.code for g in geofences}
    enricher = SilverEnricher(
        bogota.vehicles(),
        GeofenceIndex(geofences),
        PipelineParameters(),
        {sv.vehicle.vehicle_id: sv.route for sv in bogota.scenario(START)},
    )
    expected = {
        (
            a.vehicle_id,
            a.event_ts,
            a.alert_type.value,
            a.severity.value,
            code_by_id.get(a.geofence_id),
        )
        for a in enricher.enrich(processed).alerts
    }
    assert expected, "el escenario debe generar alertas"
    assert alerts_in_window(conn(DbRole.ETL)) == expected


def test_each_scenario_vehicle_triggers_its_rule(processed, conn) -> None:
    alerts = alerts_in_window(conn(DbRole.ETL))
    by_vehicle = {vid: {(t, s) for v, _, t, s, _ in alerts if v == vid} for vid in VEHICLE_IDS}

    assert by_vehicle["VHC-8472-ABC"] == set()  # conductor cumplido
    assert {("SPEEDING", "HIGH"), ("SPEEDING", "CRITICAL")} <= by_vehicle["VHC-1935-BOG"]
    assert ("LOW_FUEL_RANGE", "CRITICAL") in by_vehicle["VHC-7310-TRZ"]
    school = {code for v, _, t, _, code in alerts if v == "VHC-5521-KLM" and t == "SPEEDING"}
    assert any(code and code.startswith("ESC-") for code in school), school


def test_refuel_detected_in_silver(processed, conn) -> None:
    refuels = (
        conn(DbRole.ETL)
        .execute(
            """
        SELECT count(*) FROM silver.telemetry_enriched
        WHERE vehicle_id = 'VHC-2048-QWE' AND is_refuel AND event_ts >= %s AND event_ts < %s
        """,
            WINDOW,
        )
        .fetchone()[0]
    )
    assert refuels == 1


def test_every_ingested_point_reaches_silver(processed, conn) -> None:
    count = (
        conn(DbRole.ETL)
        .execute(
            """
        SELECT count(*) FROM silver.telemetry_enriched
        WHERE vehicle_id = ANY(%s) AND event_ts >= %s AND event_ts < %s
        """,
            (VEHICLE_IDS, *WINDOW),
        )
        .fetchone()[0]
    )
    assert count == len(processed)


def test_gold_driver_score_ranks_drivers(processed, conn) -> None:
    """Oro refrescado: el conductor agresivo puntúa peor que el cumplido."""
    rows = dict(
        conn(DbRole.ADMIN)
        .execute(
            """
            SELECT vehicle_id, driver_score FROM gold.vehicle_daily_kpi
            WHERE local_day = (%s AT TIME ZONE 'America/Bogota')::date
              AND vehicle_id IN ('VHC-8472-ABC', 'VHC-1935-BOG')
            """,
            (START,),
        )
        .fetchall()
    )
    assert rows["VHC-1935-BOG"] < rows["VHC-8472-ABC"]


def test_second_run_is_a_noop(processed, conn, etl_conn) -> None:
    reader = conn(DbRole.ETL)
    before = alerts_in_window(reader)
    report = BronzeToSilverWorker(etl_conn, CONFIG).run_once()
    assert report.vehicles == 0
    assert alerts_in_window(reader) == before


def test_late_point_is_enriched_and_alerted(processed, settings, conn, etl_conn) -> None:
    """Un paquete que llega tarde (event_ts antiguo, ingested_at nuevo) se procesa
    con el estado de la traza y dispara su alerta."""
    compliant = sorted(
        (p for p in processed if p.vehicle_id == "VHC-8472-ABC"), key=lambda p: p.event_ts
    )
    anchor = compliant[len(compliant) // 2]
    late = TelemetryPoint(
        anchor.vehicle_id,
        anchor.event_ts + timedelta(milliseconds=500),
        anchor.lat,
        anchor.lon,
        110.0,  # muy por encima de cualquier límite de Bogotá
        anchor.fuel_level_pct,
    )
    pools = PoolRegistry(settings, max_size=1)
    try:
        assert PostgresRepository(pools).insert_raw([late], source="test") == 1
    finally:
        pools.close()

    report = BronzeToSilverWorker(etl_conn, CONFIG).run_once()
    assert report.vehicles == 1 and report.new_rows == 1

    speeding = {
        ts
        for v, ts, t, _, _ in alerts_in_window(conn(DbRole.ETL))
        if v == late.vehicle_id and t == "SPEEDING"
    }
    assert late.event_ts in speeding
