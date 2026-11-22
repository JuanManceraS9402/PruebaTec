"""Benchmark de Oro: reescritura de definiciones e informe, sin base de datos."""

from __future__ import annotations

from gps_flota.benchmark.gold import QueryResult, _to_bench, dashboard_query, render
from gps_flota.benchmark.runner import BenchmarkConfig


def test_definitions_read_benchmark_tables_only() -> None:
    sql = _to_bench(
        "SELECT 'x'::silver.alert_type FROM silver.telemetry_enriched t "
        "JOIN silver.alert a ON true JOIN ref.vehicle v ON true "
        "JOIN gold.infraction_point p ON true "
        "JOIN security.vehicle_pseudonym vp ON true "
        "JOIN ref.geofence g ON true WHERE gold.driver_score(1, 2, 3) > 0"
    )
    assert "bench_gold.telemetry_enriched" in sql
    assert "bench_gold.alert a" in sql
    assert "bench_gold.vehicle v" in sql
    assert "bench_gold.infraction_point" in sql
    assert "bench_gold.vehicle_pseudonym" in sql
    # tipos, geocercas reales y funciones no se reescriben
    assert "silver.alert_type" in sql and "ref.geofence" in sql and "gold.driver_score" in sql


def test_dashboard_queries_use_masked_benchmark_views() -> None:
    assert dashboard_query("SELECT 1 FROM analytics.infraction_map m") == (
        "SELECT 1 FROM bench_gold.v_infraction_map m"
    )


def test_report_flags_queries_over_target() -> None:
    env = {"PostgreSQL": "19", "PostGIS": "3.7", "shared_buffers": "128MB", "work_mem": "4MB"}
    report = render(
        BenchmarkConfig(vehicles=5, days=1),
        7200,
        100,
        {"vehicle_daily_kpi": 1.0},
        [QueryResult("rápida", 5.0, 1, 0.1), QueryResult("lenta", 250.0, 1, 0.1)],
        {**env, "Fecha": "hoy"},
    )
    assert "| rápida | **5.0 ms** | 1 | 0.10 MB | sí |" in report
    assert "| lenta | **250.0 ms** | 1 | 0.10 MB | no |" in report
