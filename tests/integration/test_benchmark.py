"""Benchmark en miniatura: valida el SQL del generador, las variantes y el informe."""

from __future__ import annotations

import pytest

from gps_flota.benchmark.runner import QUALIFIED, SCHEMA, BenchmarkConfig, render_markdown, run
from gps_flota.config import DbRole
from gps_flota.db import connect

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def result(settings, db_available, seeded):
    cfg = BenchmarkConfig(vehicles=5, days=2, interval_s=600, repetitions=1)
    with connect(DbRole.OWNER, settings, autocommit=True) as conn:
        yield run(conn, cfg, log=lambda _: None)
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def test_generates_expected_rows(result) -> None:
    assert result.rows == result.config.expected_rows == 5 * 2 * 144


def test_copies_bronze_indexes(result) -> None:
    names = {name for name, _ in result.sizes}
    assert {"telemetry_raw_geom_gist", "telemetry_raw_event_brin"} <= names


def test_day_filter_prunes_partitions(result) -> None:
    heatmap = result.scenarios_by_key()["heatmap"]
    one_day, all_days = heatmap
    assert one_day.summary.partitions_scanned == 1
    assert all_days.summary.partitions_scanned == 2


def test_variants_restore_dropped_indexes(settings, result) -> None:
    """Las variantes 'sin índice' se deshacen: el esquema queda intacto."""
    with connect(DbRole.OWNER, settings, autocommit=True) as conn:
        bench = conn.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND tablename = 'telemetry_raw'",
            (SCHEMA,),
        ).fetchone()[0]
        bronze = conn.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'bronze' "
            "AND tablename = 'telemetry_raw'"
        ).fetchone()[0]
    assert bench == bronze


def test_report_renders(result) -> None:
    md = render_markdown(result)
    assert md.startswith("# Benchmark")
    assert QUALIFIED in md and "Sin GiST" in md
