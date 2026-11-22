"""Decisiones de rendimiento de Oro que no deben perderse."""

from __future__ import annotations

from datetime import date

import pytest

from gps_flota.benchmark.gold import main as gold_benchmark
from gps_flota.config import DbRole
from gps_flota.dashboard import data

pytestmark = pytest.mark.integration


def test_map_view_exposes_no_vehicle_id(conn, seeded) -> None:
    columns = {
        r[0]
        for r in conn(DbRole.ANALYST).execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'analytics' AND table_name = 'infraction_map'
            """
        )
    }
    assert "vehicle_key" in columns and "geom" in columns
    assert "vehicle_id" not in columns


def test_map_view_filter_reaches_the_gist_index(conn, seeded) -> None:
    c = conn(DbRole.ADMIN)
    with c.transaction(force_rollback=True):
        c.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in c.execute(
                """
                EXPLAIN SELECT 1 FROM analytics.infraction_map
                WHERE geom && ST_MakeEnvelope(-74.09, 4.63, -74.06, 4.66, 4326)
                """
            )
        )
    assert "infraction_point_geom" in plan, plan


def test_bbox_query_runs_as_analyst(conn, seeded) -> None:
    frame = data.infractions_in_bbox(conn(DbRole.ANALYST), date(2026, 1, 1), -74.2, 4.5, -74.0, 4.8)
    assert list(frame.columns) == ["vehicle_key", "event_ts", "severity", "lon", "lat"]


def test_masked_id_is_precomputed_for_every_vehicle(conn, seeded) -> None:
    rows = (
        conn(DbRole.OWNER)
        .execute(
            "SELECT vehicle_id, masked_id, security.mask_vehicle_id(vehicle_id) "
            "FROM security.vehicle_pseudonym"
        )
        .fetchall()
    )
    assert rows and all(masked == expected for _, masked, expected in rows)


def test_analyst_cannot_read_pseudonym_table(conn, seeded) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn(DbRole.ANALYST).execute("SELECT masked_id FROM security.vehicle_pseudonym")


def test_gold_benchmark_runs_end_to_end(seeded, tmp_path) -> None:
    out = tmp_path / "gold.md"
    try:
        assert (
            gold_benchmark(
                [
                    "--vehicles",
                    "3",
                    "--days",
                    "1",
                    "--repetitions",
                    "1",
                    "--interval-s",
                    "300",
                    "--out",
                    str(out),
                ]
            )
            == 0
        )
        report = out.read_text(encoding="utf-8")
        assert "KPIs y rutas del día" in report and "recuadro del mapa" in report
    finally:
        gold_benchmark(["--drop"])
