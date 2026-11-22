"""Benchmark de la capa Oro: lo que espera el tablero con millones de puntos.

python -m simon_geo.benchmark.gold            # 5 M de puntos (500 vehículos × 7 días)
python -m simon_geo.benchmark.gold --reuse    # vuelve a medir sin regenerar Plata
python -m simon_geo.benchmark.gold --drop     # borra el esquema bench_gold

Genera Plata sintética en ``bench_gold`` y construye ahí las vistas de Oro y las vistas
enmascaradas **a partir de sus definiciones reales** (catálogo de PostgreSQL), con sus
índices. Las consultas son las del tablero (``dashboard.data``), ejecutadas como
``bi_analyst``, y el tiempo es el que ve el cliente: consulta + transferencia de filas.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final

import psycopg

from simon_geo.benchmark.runner import BenchmarkConfig, environment
from simon_geo.config import DbRole
from simon_geo.dashboard import data
from simon_geo.db import connect

SCHEMA: Final = "bench_gold"
GOLD_VIEWS: Final = (
    "vehicle_daily_kpi",
    "infraction_point",
    "infraction_grid_250m",
    "infraction_hotspot",
)
TARGET_MS: Final = 100.0  # umbral de "responde en milisegundos" para un tablero


@dataclass(frozen=True, slots=True)
class QueryResult:
    title: str
    median_ms: float
    rows: int
    payload_mb: float


def _to_bench(sql: str) -> str:
    """Reescribe una definición para que lea las tablas de ``bench_gold``."""
    sql = re.sub(r"\bsilver\.telemetry_enriched\b", f"{SCHEMA}.telemetry_enriched", sql)
    sql = re.sub(r"\bsilver\.alert\b(?!_)", f"{SCHEMA}.alert", sql)
    sql = re.sub(r"\bref\.vehicle\b", f"{SCHEMA}.vehicle", sql)
    sql = re.sub(r"\bsecurity\.vehicle_pseudonym\b", f"{SCHEMA}.vehicle_pseudonym", sql)
    for view in GOLD_VIEWS:
        sql = re.sub(rf"\bgold\.{view}\b", f"{SCHEMA}.{view}", sql)
    return sql


def dashboard_query(sql: str) -> str:
    """Consulta del tablero apuntando a las vistas enmascaradas de benchmark."""
    return re.sub(r"\banalytics\.(\w+)", rf"{SCHEMA}.v_\1", sql)


_LOAD_DAY: Final = f"""
INSERT INTO {SCHEMA}.telemetry_enriched (
    vehicle_id, event_ts, geom, speed_kmh, fuel_level_pct, prev_event_ts, delta_s,
    segment_distance_m, implied_speed_kmh, accel_mps2, fuel_delta_pct, is_harsh_event,
    applied_speed_limit_kmh, speed_excess_kmh, is_speeding)
SELECT
    format('BEN-%%s-BEN', lpad(v::text, 4, '0')), ts,
    ST_SetSRID(ST_MakePoint(
        -74.11 + 0.08 * cos(v * 2.3) + 0.03 * cos(extract(epoch FROM ts) / 1800.0 + v),
        4.65 + 0.12 * sin(v * 1.7) + 0.03 * sin(extract(epoch FROM ts) / 1800.0 + v)), 4326),
    s.speed, 80, ts - make_interval(secs => %(step)s), %(step)s,
    s.speed / 3.6 * %(step)s, s.speed, 0, -0.01, random() < 0.0005,
    50, greatest(s.speed - 50, 0), s.speed > 53
FROM generate_series(%(start)s::timestamptz,
                     %(start)s::timestamptz + interval '1 day' - make_interval(secs => %(step)s),
                     make_interval(secs => %(step)s)) AS ts
CROSS JOIN generate_series(1, %(vehicles)s) AS v
CROSS JOIN LATERAL (
    SELECT greatest(0, 35 + 25 * sin(extract(epoch FROM ts) / 300.0 + v) + random() * 5)::real
           AS speed
) s
"""

# ~2,5 % de los puntos genera alerta, la proporción del escenario de demo
_LOAD_ALERTS: Final = f"""
INSERT INTO {SCHEMA}.alert
    (vehicle_id, event_ts, alert_type, severity, geofence_id, geom, observed_value,
     threshold_value)
SELECT vehicle_id, event_ts, 'SPEEDING',
       (CASE WHEN speed_excess_kmh < 3 THEN 'LOW' WHEN speed_excess_kmh < 6 THEN 'MEDIUM'
             WHEN speed_excess_kmh < 9 THEN 'HIGH' ELSE 'CRITICAL'
        END)::silver.alert_severity,
       (SELECT geofence_id FROM ref.geofence g WHERE g.active AND ST_Intersects(g.geom, t.geom)
        ORDER BY g.geofence_id DESC LIMIT 1),
       geom, speed_kmh, 50
FROM {SCHEMA}.telemetry_enriched t
WHERE is_speeding AND random() < %(share)s
"""


def prepare(conn: psycopg.Connection, cfg: BenchmarkConfig, log: Callable[[str], None]) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.execute(
        f"CREATE UNLOGGED TABLE {SCHEMA}.telemetry_enriched "
        "(LIKE silver.telemetry_enriched INCLUDING DEFAULTS)"
    )
    conn.execute(
        f"CREATE UNLOGGED TABLE {SCHEMA}.alert "
        "(LIKE silver.alert INCLUDING DEFAULTS INCLUDING IDENTITY)"
    )
    conn.execute(f"CREATE TABLE {SCHEMA}.vehicle (LIKE ref.vehicle INCLUDING DEFAULTS)")
    conn.execute(f"""
        INSERT INTO {SCHEMA}.vehicle (vehicle_id, fuel_tank_capacity_l, nominal_consumption_l_100km)
        SELECT format('BEN-%s-BEN', lpad(v::text, 4, '0')), 200, 30
        FROM generate_series(1, {cfg.vehicles}) AS v
    """)
    conn.execute(f"""
        CREATE TABLE {SCHEMA}.vehicle_pseudonym AS
        SELECT vehicle_id, 'VK-' || left(md5(vehicle_id), 12) AS vehicle_key,
               security.mask_vehicle_id(vehicle_id) AS masked_id
        FROM {SCHEMA}.vehicle
    """)
    conn.execute(f"ALTER TABLE {SCHEMA}.vehicle_pseudonym ADD PRIMARY KEY (vehicle_id)")
    for d in range(cfg.days):
        day = cfg.first_day + timedelta(days=d)
        conn.execute(
            _LOAD_DAY,
            {"start": f"{day} 00:00+00", "step": cfg.interval_s, "vehicles": cfg.vehicles},
        )
        log(f"  Plata {day}")
    conn.execute(f"ANALYZE {SCHEMA}.telemetry_enriched")
    total, speeding = conn.execute(
        f"SELECT count(*), count(*) FILTER (WHERE is_speeding) FROM {SCHEMA}.telemetry_enriched"
    ).fetchone()
    conn.execute(_LOAD_ALERTS, {"share": min(1.0, 0.025 * total / max(speeding, 1))})
    conn.execute(f"ANALYZE {SCHEMA}.alert")


def build_gold(conn: psycopg.Connection, log: Callable[[str], None]) -> dict[str, float]:
    """Crea las vistas de Oro desde el catálogo y mide cuánto tarda cada una."""
    seconds: dict[str, float] = {}
    for view in GOLD_VIEWS:
        conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {SCHEMA}.{view} CASCADE")
        definition = conn.execute(
            "SELECT pg_get_viewdef(%s::regclass, true)", (f"gold.{view}",)
        ).fetchone()[0]
        started = time.perf_counter()
        conn.execute(
            f"CREATE MATERIALIZED VIEW {SCHEMA}.{view} AS "
            f"{_to_bench(definition).rstrip().rstrip(';')} WITH DATA"
        )
        indexes = conn.execute(
            "SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indrelid = %s::regclass",
            (f"gold.{view}",),
        ).fetchall()
        for (index,) in indexes:
            conn.execute(index.replace(f" ON gold.{view} ", f" ON {SCHEMA}.{view} "))
        conn.execute(f"ANALYZE {SCHEMA}.{view}")
        seconds[view] = round(time.perf_counter() - started, 1)
        log(f"  {view}: {seconds[view]} s")
        masked = conn.execute(
            "SELECT pg_get_viewdef(%s::regclass, true)", (f"analytics.{view}",)
        ).fetchone()[0]
        conn.execute(
            f"CREATE VIEW {SCHEMA}.v_{view} WITH (security_barrier = true) AS "
            f"{_to_bench(masked).rstrip().rstrip(';')}"
        )
    map_view = conn.execute(
        "SELECT pg_get_viewdef('analytics.infraction_map'::regclass, true)"
    ).fetchone()[0]
    conn.execute(f"CREATE VIEW {SCHEMA}.v_infraction_map AS {_to_bench(map_view)}")
    conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO bi_analyst")
    conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO bi_analyst")
    return seconds


def scenarios(day: date) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("Días disponibles (selector)", data._DAYS, {}),
        ("KPIs y rutas del día, toda la flota", data._DAILY_KPI, {"day": day}),
        ("Infracciones del día", data._INFRACTIONS, {"day": day}),
        (
            "Infracciones de un día en el recuadro del mapa (GiST)",
            data._INFRACTIONS_IN_BBOX,
            {"day": day, "west": -74.09, "south": 4.63, "east": -74.06, "north": 4.66},
        ),
        (
            "Top 10 peores conductores del día",
            """SELECT vehicle_id, vehicle_key, driver_score FROM analytics.vehicle_daily_kpi
               WHERE local_day = %(day)s ORDER BY driver_score, vehicle_key LIMIT 10""",
            {"day": day},
        ),
        ("Zonas calientes", data._HOTSPOTS, {}),
        (
            "Malla de calor de 250 m",
            """SELECT cell_x, cell_y, sum(infractions) FROM analytics.infraction_grid_250m
               GROUP BY 1, 2""",
            {},
        ),
    ]


def measure(conn: psycopg.Connection, day: date, repetitions: int) -> list[QueryResult]:
    conn.execute("SET ROLE bi_analyst")
    results = []
    try:
        for title, sql, params in scenarios(day):
            query = dashboard_query(sql)
            times, rows, payload = [], 0, 0
            conn.execute(query, params).fetchall()  # calienta la caché
            for _ in range(repetitions):
                started = time.perf_counter()
                fetched = conn.execute(query, params).fetchall()
                times.append((time.perf_counter() - started) * 1000)
                rows = len(fetched)
            payload = sum(len(str(v)) for row in fetched for v in row)
            results.append(
                QueryResult(title, round(statistics.median(times), 1), rows, payload / 1e6)
            )
    finally:
        conn.execute("RESET ROLE")
    return results


def render(
    cfg: BenchmarkConfig,
    silver_rows: int,
    alerts: int,
    build: dict[str, float],
    results: list[QueryResult],
    env: dict[str, str],
) -> str:
    lines = [
        "# Benchmark de la capa Oro",
        "",
        "> Generado con `python -m simon_geo.benchmark.gold`. Mide lo que espera el tablero:",
        "> consulta más transferencia de filas, como `bi_analyst` y a través de las vistas",
        "> enmascaradas. Los números dependen de la máquina.",
        "",
        f"* **{silver_rows:,} puntos** en Plata ({cfg.vehicles} vehículos × {cfg.days} días, "
        f"un reporte cada {cfg.interval_s} s) y **{alerts:,} alertas**.",
        f"* PostgreSQL {env['PostgreSQL']}, PostGIS {env['PostGIS']}, configuración por "
        f"defecto (`shared_buffers` {env['shared_buffers']}, `work_mem` {env['work_mem']}).",
        f"* Fecha: {env['Fecha']}.",
        "",
        "## Consultas del tablero",
        "",
        f"| Consulta | Tiempo (mediana) | Filas | Datos | < {TARGET_MS:.0f} ms |",
        "|---|---:|---:|---:|:---:|",
    ]
    lines += [
        f"| {r.title} | **{r.median_ms:,.1f} ms** | {r.rows:,} | {r.payload_mb:,.2f} MB | "
        f"{'sí' if r.median_ms < TARGET_MS else 'no'} |"
        for r in results
    ]
    lines += [
        "",
        "## Construcción de Oro (refresco completo)",
        "",
        "| Vista | Tiempo |",
        "|---|---:|",
    ]
    lines += [f"| `gold.{view}` | {s:,.1f} s |" for view, s in build.items()]
    lines += [f"| **Total** | **{sum(build.values()):,.1f} s** |", ""]
    lines += [
        "## Cómo leerlo",
        "",
        "* Los datos son sintéticos. Sus alertas cubren la ciudad de forma casi uniforme, así",
        "  que DBSCAN (3 excesos a menos de 600 m) las une en muy pocas zonas: el tiempo de",
        "  *Zonas calientes* no representa un caso con muchas zonas.",
        "* Las consultas leen vistas materializadas: el costo se paga al refrescar, no al leer.",
        "  El refresco es completo y crece con la historia (ver DESIGN.md, sección 8).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark de la capa Oro")
    parser.add_argument("--vehicles", type=int, default=500)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--interval-s", type=int, default=60)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--reuse", action="store_true", help="no regenerar Plata")
    parser.add_argument("--drop", action="store_true", help=f"borrar {SCHEMA} y salir")
    parser.add_argument("--out", type=Path, default=Path("docs/benchmark_gold.md"))
    args = parser.parse_args(argv)

    with connect(DbRole.OWNER, autocommit=True) as conn:
        if args.drop:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            print(f"Esquema {SCHEMA} eliminado.")
            return 0
        cfg = BenchmarkConfig(args.vehicles, args.days, args.interval_s, args.repetitions)
        exists = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"{SCHEMA}.telemetry_enriched",)
        ).fetchone()[0]
        if args.reuse and exists:
            first = conn.execute(
                f"SELECT min(event_ts)::date FROM {SCHEMA}.telemetry_enriched"
            ).fetchone()[0]
            cfg = BenchmarkConfig(
                args.vehicles, args.days, args.interval_s, args.repetitions, first
            )
        else:
            print(f"Generando {cfg.expected_rows:,} puntos en {SCHEMA}…", flush=True)
            prepare(conn, cfg, lambda m: print(m, flush=True))
        print("Construyendo Oro…", flush=True)
        build = build_gold(conn, lambda m: print(m, flush=True))
        results = measure(conn, cfg.mid_day, cfg.repetitions)
        for r in results:
            print(
                f"{r.median_ms:>10,.1f} ms  {r.rows:>7,} filas  {r.payload_mb:>7.2f} MB  {r.title}"
            )
        silver_rows, alerts = conn.execute(
            f"SELECT (SELECT count(*) FROM {SCHEMA}.telemetry_enriched), "
            f"(SELECT count(*) FROM {SCHEMA}.alert)"
        ).fetchone()
        report = render(cfg, silver_rows, alerts, build, results, environment(conn))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"Informe escrito en {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
