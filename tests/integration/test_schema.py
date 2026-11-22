from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gps_flota.config import DbRole
from gps_flota.parameters import PipelineParameters
from gps_flota.spatial.crs import lonlat_to_metric

pytestmark = pytest.mark.integration


def test_postgis_installed(conn) -> None:
    version = conn(DbRole.OWNER).execute("SELECT postgis_lib_version()").fetchone()[0]
    assert version.startswith("3.")


def test_epsg_9377_matches_pyproj(conn) -> None:
    """PostGIS y pyproj proyectan igual a EPSG:9377 (misma geometría en BD y en Python)."""
    x, y = (
        conn(DbRole.OWNER)
        .execute(
            """
        SELECT ST_X(p), ST_Y(p) FROM (
            SELECT ST_Transform(ST_SetSRID(ST_MakePoint(-74.08, 4.65), 4326), 9377) AS p
        ) s
        """
        )
        .fetchone()
    )
    px, py = lonlat_to_metric(-74.08, 4.65)
    assert x == pytest.approx(px, abs=1.0)
    assert y == pytest.approx(py, abs=1.0)


def test_sql_parameters_match_python_defaults(conn) -> None:
    """ops.parameter y PipelineParameters no pueden divergir."""
    rows = conn(DbRole.ADMIN).execute("SELECT name, value FROM ops.parameter").fetchall()
    from_db = PipelineParameters.from_mapping(dict(rows))
    assert from_db == PipelineParameters()
    assert {name for name, _ in rows} == set(PipelineParameters.__dataclass_fields__)


@pytest.mark.parametrize("parent", ["bronze.telemetry_raw", "silver.telemetry_enriched"])
def test_daily_partitions_created_at_init(conn, parent: str) -> None:
    today = datetime.now(UTC).date()
    name = parent.split(".")[1] + "_p" + today.strftime("%Y%m%d")
    schema = parent.split(".")[0]
    exists = conn(DbRole.OWNER).execute("SELECT to_regclass(%s)", (f"{schema}.{name}",)).fetchone()
    assert exists[0] is not None


def test_ensure_partitions_is_idempotent(conn) -> None:
    day = (datetime.now(UTC) + timedelta(days=200)).date()
    c = conn(DbRole.ETL)
    sql = "SELECT ops.ensure_daily_partitions('silver.telemetry_enriched', %s, %s)"
    c.execute(sql, (day, day))
    assert c.execute(sql, (day, day)).fetchone()[0] == 0


def test_ensure_partitions_rejects_non_partitioned_table(conn) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.RaiseException, match="no es una tabla particionada"):
        conn(DbRole.OWNER).execute(
            "SELECT ops.ensure_daily_partitions('ref.vehicle', current_date, current_date)"
        )


@pytest.mark.parametrize(
    ("speeding_pct", "harsh_100km", "max_excess", "expected"),
    [
        (0, 0, 0, 100),
        (10, 2, 10, 75),  # 100 - 10 - 10 - 5
        (100, 10, 60, 0),  # todas las penalizaciones topadas → 0
        (None, None, None, 100),
    ],
)
def test_driver_score(conn, speeding_pct, harsh_100km, max_excess, expected) -> None:
    score = (
        conn(DbRole.OWNER)
        .execute("SELECT gold.driver_score(%s, %s, %s)", (speeding_pct, harsh_100km, max_excess))
        .fetchone()[0]
    )
    assert float(score) == expected


def test_etl_can_refresh_gold(conn) -> None:
    conn(DbRole.ETL).execute("SELECT gold.refresh_all(true)")
