"""Ingesta real en Bronce y consulta espacial de geocercas en PostGIS."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from simon_geo.api.repository import PostgresRepository
from simon_geo.config import DbRole
from simon_geo.db import PoolRegistry
from simon_geo.domain import TelemetryPoint
from simon_geo.processing.queries import BRONZE_POINTS_WITH_GEOFENCES
from simon_geo.seed import bogota
from simon_geo.simulator.fleet import FleetSimulator
from simon_geo.spatial.geofencing import GeofenceIndex

pytestmark = pytest.mark.integration


@pytest.fixture
def repository(settings, db_available):
    pools = PoolRegistry(settings, max_size=2)
    yield PostgresRepository(pools)
    pools.close()


def unique_ts(offset: timedelta = timedelta(0)) -> datetime:
    # microsegundos del reloj → no colisiona entre ejecuciones locales repetidas
    return datetime.now(UTC) + offset


def make_point(ts: datetime, lat: float = 4.65, lon: float = -74.08) -> TelemetryPoint:
    return TelemetryPoint("VHC-8472-ABC", ts, lat, lon, 40.0, 50.0)


def test_insert_is_idempotent(repository, seeded) -> None:
    t = unique_ts()
    points = [make_point(t), make_point(t + timedelta(seconds=3))]
    assert repository.insert_raw(points, source="test") == 2
    assert repository.insert_raw(points, source="test") == 0


def test_generated_geometry_has_lon_lat_order(repository, conn, seeded) -> None:
    t = unique_ts()
    repository.insert_raw([make_point(t, lat=4.6123, lon=-74.1456)], source="test")
    x, y, srid = (
        conn(DbRole.ADMIN)
        .execute(
            """
        SELECT ST_X(geom), ST_Y(geom), ST_SRID(geom) FROM bronze.telemetry_raw
        WHERE vehicle_id = 'VHC-8472-ABC' AND event_ts = %s
        """,
            (t,),
        )
        .fetchone()
    )
    assert (x, y, srid) == (pytest.approx(-74.1456), pytest.approx(4.6123), 4326)


def test_missing_partition_is_created_on_ingest(repository, conn, seeded) -> None:
    """Un paquete de un día sin partición no cae en la DEFAULT."""
    t = unique_ts(timedelta(days=-20))
    repository.insert_raw([make_point(t)], source="test")
    partition = (
        conn(DbRole.ADMIN)
        .execute(
            """
        SELECT tableoid::regclass::text FROM bronze.telemetry_raw
        WHERE vehicle_id = 'VHC-8472-ABC' AND event_ts = %s
        """,
            (t,),
        )
        .fetchone()[0]
    )
    assert partition == f"bronze.telemetry_raw_p{t.strftime('%Y%m%d')}"


def test_postgis_geofences_match_python_index(repository, conn, seeded) -> None:
    """La consulta SQL (ST_Intersects + GiST) y el índice en memoria (STRtree)
    asignan exactamente las mismas geocercas a cada punto del escenario."""
    start = unique_ts(timedelta(hours=-3)).replace(microsecond=0)
    index = GeofenceIndex(bogota.geofences())
    points = list(dict.fromkeys(FleetSimulator(bogota.scenario(start), index).run()))
    repository.insert_raw(points, source="test")

    rows = (
        conn(DbRole.ETL)
        .execute(
            BRONZE_POINTS_WITH_GEOFENCES,
            {
                "vehicle_ids": sorted({p.vehicle_id for p in points}),
                "from_ts": start,
                "to_ts": max(p.event_ts for p in points) + timedelta(seconds=1),
            },
        )
        .fetchall()
    )

    code_by_db_id = {db_id: code for code, db_id in seeded.items()}
    from_sql = {(r[0], r[1]): {code_by_db_id[i] for i in r[6]} for r in rows}
    from_python = {
        (p.vehicle_id, p.event_ts): {g.code for g in index.containing(p.lon, p.lat)} for p in points
    }
    assert len(from_sql) == len(from_python)
    assert from_sql == from_python
    assert any(len(codes) > 1 for codes in from_sql.values())  # hubo solapes reales
