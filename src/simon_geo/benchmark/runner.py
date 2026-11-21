"""Benchmark Big Data: millones de puntos GPS y consultas analíticas típicas.

Los datos se generan en el servidor (``generate_series``) en una tabla
``bench.telemetry_raw`` con la MISMA definición, particiones e índices que
``bronze.telemetry_raw`` (se copian del catálogo, no se reescriben a mano). Así
el benchmark mide el esquema real sin contaminar Bronce: el worker no procesa
estos datos.

Cada consulta se mide con ``EXPLAIN (ANALYZE, BUFFERS)`` en varias variantes
(con índices, sin el índice GiST o BRIN, sin filtro de tiempo / sin poda de
particiones). Las variantes "sin índice" borran el índice dentro de una
transacción que se deshace al terminar: el esquema queda intacto.
"""

from __future__ import annotations

import json
import os
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import psycopg

from simon_geo.benchmark.plan import PlanSummary, median_ms, summarize

SCHEMA: Final = "bench"
TABLE: Final = "telemetry_raw"
QUALIFIED: Final = f"{SCHEMA}.{TABLE}"

# Índices de la tabla de benchmark (mismos nombres que en Bronce, otro esquema)
GIST_INDEX: Final = f"{SCHEMA}.telemetry_raw_geom_gist"
BRIN_INDEX: Final = f"{SCHEMA}.telemetry_raw_event_brin"


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    vehicles: int = 500
    days: int = 7
    interval_s: int = 60
    repetitions: int = 3
    first_day: date = field(default_factory=lambda: datetime.now(UTC).date() - timedelta(days=60))

    def __post_init__(self) -> None:
        if min(self.vehicles, self.days, self.interval_s, self.repetitions) <= 0:
            raise ValueError("vehicles, days, interval_s y repetitions deben ser positivos")
        if self.vehicles > 9999:
            raise ValueError("Máximo 9999 vehículos (el ID usa 4 dígitos)")

    @property
    def expected_rows(self) -> int:
        return self.vehicles * self.days * (86_400 // self.interval_s)

    @property
    def mid_day(self) -> date:
        return self.first_day + timedelta(days=self.days // 2)


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    query: str
    drop_index: str | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    title: str
    question: str
    variants: tuple[Variant, ...]


@dataclass(slots=True)
class VariantResult:
    variant: Variant
    median_ms: float
    summary: PlanSummary
    text_plan: str


@dataclass(slots=True)
class BenchmarkResult:
    config: BenchmarkConfig
    rows: int
    load_s: float
    environment: dict[str, str]
    sizes: list[tuple[str, int]]
    btree_equivalent_bytes: int
    scenarios: list[tuple[Scenario, list[VariantResult]]]

    def scenarios_by_key(self) -> dict[str, list[VariantResult]]:
        return {scenario.key: variants for scenario, variants in self.scenarios}


# ----------------------------------------------------------------- escenarios
def scenarios(cfg: BenchmarkConfig) -> list[Scenario]:
    """Consultas que hace BI sobre telemetría. Los literales vienen de la config,
    no de usuarios (son fechas e IDs generados), por eso se interpolan."""
    day = cfg.mid_day.isoformat()
    next_day = (cfg.mid_day + timedelta(days=1)).isoformat()
    day_range = f"b.event_ts >= '{day}' AND b.event_ts < '{next_day}'"
    hour_range = f"b.event_ts >= '{day} 15:00+00' AND b.event_ts < '{day} 16:00+00'"
    # Viewport de mapa en el centro de Bogotá (~3 km × 3 km)
    viewport = "ST_MakeEnvelope(-74.09, 4.63, -74.06, 4.66, 4326)"
    school = "(SELECT geom FROM ref.geofence WHERE code = 'BOG-ESC-TEUSAQUILLO')"

    points_in_zone = f"""
        SELECT count(*) FROM {QUALIFIED} b
        WHERE ST_Intersects(b.geom, {school}) AND {{time}}"""
    viewport_hour = f"""
        SELECT b.vehicle_id, b.event_ts, b.speed_kmh FROM {QUALIFIED} b
        WHERE b.geom && {viewport} AND {hour_range}"""
    trace = f"""
        SELECT b.event_ts, b.geom, b.speed_kmh FROM {QUALIFIED} b
        WHERE b.vehicle_id = 'BEN-0042-BEN' AND {day_range}
        ORDER BY b.event_ts"""
    enrichment_hour = f"""
        SELECT count(*), sum(cardinality(gf.geofence_ids)) FROM {QUALIFIED} b
        LEFT JOIN LATERAL (
            SELECT array_agg(g.geofence_id) AS geofence_ids FROM ref.geofence g
            WHERE g.active AND ST_Intersects(g.geom, b.geom)
        ) gf ON true
        WHERE {hour_range}"""
    heatmap = f"""
        SELECT floor(ST_X(p) / 250) AS cx, floor(ST_Y(p) / 250) AS cy, count(*)
        FROM (
            SELECT ST_Transform(b.geom, 9377) AS p FROM {QUALIFIED} b
            WHERE b.speed_kmh > 50 AND {{time}}
        ) s
        GROUP BY 1, 2"""

    return [
        Scenario(
            "zone",
            "Puntos dentro de una geocerca en un día",
            "¿Cuántos reportes hubo dentro de la zona escolar de Teusaquillo ayer?",
            (
                Variant("Con índices (GiST + BRIN + poda)", points_in_zone.format(time=day_range)),
                Variant(
                    "Sin GiST",
                    points_in_zone.format(time=day_range),
                    drop_index=GIST_INDEX,
                ),
                Variant(
                    f"Sin filtro de tiempo ({cfg.days} días, sin poda)",
                    points_in_zone.format(time="true"),
                ),
            ),
        ),
        Scenario(
            "viewport",
            "Viewport del mapa en una hora",
            "¿Qué vehículos pasaron por este recuadro del mapa entre las 10:00 y las 11:00?",
            (
                Variant("Con índices", viewport_hour),
                Variant("Sin GiST", viewport_hour, drop_index=GIST_INDEX),
                Variant("Sin BRIN", viewport_hour, drop_index=BRIN_INDEX),
            ),
        ),
        Scenario(
            "trace",
            "Traza de un vehículo en un día",
            "Ruta recorrida por un vehículo en un día (capa de línea del tablero).",
            (Variant("Con índices (PK + poda)", trace),),
        ),
        Scenario(
            "enrichment",
            "Enriquecimiento de geocercas de una hora (consulta del worker)",
            "Geocercas de cada punto con LEFT JOIN LATERAL + ST_Intersects.",
            (Variant("Con índices", enrichment_hour),),
        ),
        Scenario(
            "heatmap",
            "Malla de calor de 250 m de excesos en un día",
            "Agregación en EPSG:9377 de los puntos a más de 50 km/h.",
            (
                Variant("Un día (poda a 1 partición)", heatmap.format(time=day_range)),
                Variant(f"{cfg.days} días (sin poda)", heatmap.format(time="true")),
            ),
        ),
    ]


# ------------------------------------------------------------------ preparación
def prepare_schema(conn: psycopg.Connection, cfg: BenchmarkConfig) -> None:
    """Crea ``bench.telemetry_raw`` como copia estructural de Bronce."""
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.execute(
        f"""
        CREATE TABLE {QUALIFIED} (
            LIKE bronze.telemetry_raw INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING CONSTRAINTS
        ) PARTITION BY RANGE (event_ts)
        """
    )
    conn.execute(f"ALTER TABLE {QUALIFIED} ADD PRIMARY KEY (vehicle_id, event_ts)")
    # Índices secundarios copiados del catálogo: si cambian en 03_bronze.sql, el
    # benchmark los sigue automáticamente.
    defs = conn.execute(
        """
        SELECT pg_get_indexdef(i.indexrelid)
        FROM pg_index i
        WHERE i.indrelid = 'bronze.telemetry_raw'::regclass AND NOT i.indisprimary
        """
    ).fetchall()
    if not defs:
        raise RuntimeError("bronze.telemetry_raw no tiene índices secundarios que copiar")
    for (definition,) in defs:
        conn.execute(
            definition.replace("ON ONLY bronze.telemetry_raw", f"ON {QUALIFIED}").replace(
                "ON bronze.telemetry_raw", f"ON {QUALIFIED}"
            )
        )
    conn.execute(
        "SELECT ops.ensure_daily_partitions(%s::regclass, %s, %s)",
        (QUALIFIED, cfg.first_day, cfg.first_day + timedelta(days=cfg.days - 1)),
    )


_LOAD_DAY: Final = f"""
INSERT INTO {QUALIFIED} (vehicle_id, event_ts, lat, lon, speed_kmh, fuel_level_pct, source)
SELECT
    format('BEN-%%s-BEN', lpad(v::text, 4, '0')),
    ts,
    -- cada vehículo recorre un circuito propio dentro de Bogotá
    4.65 + 0.12 * sin(v * 1.7) + 0.03 * sin(extract(epoch FROM ts) / 1800.0 + v),
    -74.11 + 0.08 * cos(v * 2.3) + 0.03 * cos(extract(epoch FROM ts) / 1800.0 + v),
    greatest(0, 35 + 25 * sin(extract(epoch FROM ts) / 300.0 + v) + random() * 5)::real,
    (100 - mod(extract(epoch FROM ts)::bigint / 3600, 24) * 3)::real,
    'benchmark'
FROM generate_series(%(start)s::timestamptz,
                     %(start)s::timestamptz + interval '1 day' - make_interval(secs => %(step)s),
                     make_interval(secs => %(step)s)) AS ts
CROSS JOIN generate_series(1, %(vehicles)s) AS v
-- orden de llegada realista (por tiempo): es lo que hace eficiente al BRIN
ORDER BY ts, v
"""


def load_data(conn: psycopg.Connection, cfg: BenchmarkConfig, log: Callable[[str], None]) -> float:
    started = time.perf_counter()
    for d in range(cfg.days):
        day = cfg.first_day + timedelta(days=d)
        t0 = time.perf_counter()
        conn.execute(
            _LOAD_DAY,
            {
                "start": datetime(day.year, day.month, day.day, tzinfo=UTC),
                "step": cfg.interval_s,
                "vehicles": cfg.vehicles,
            },
        )
        log(f"  día {day}: {time.perf_counter() - t0:.1f} s")
    conn.execute(f"VACUUM (ANALYZE) {QUALIFIED}")
    return time.perf_counter() - started


# ------------------------------------------------------------------- medición
def explain(
    conn: psycopg.Connection, query: str, repetitions: int
) -> tuple[float, PlanSummary, str]:
    summaries = []
    for _ in range(repetitions):
        raw = conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}").fetchone()[0]
        plan = raw if isinstance(raw, list) else json.loads(raw)
        summaries.append(summarize(plan, partitioned_table=TABLE))
    text = "\n".join(
        r[0] for r in conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) {query}")
    )
    return median_ms(summaries), summaries[-1], text


def run_variant(conn: psycopg.Connection, variant: Variant, repetitions: int) -> VariantResult:
    with conn.transaction(force_rollback=True):
        if variant.drop_index:
            conn.execute(f"DROP INDEX {variant.drop_index}")
        # Una lectura previa calienta la caché: se mide el plan, no el disco frío.
        conn.execute(variant.query).fetchall()
        ms, summary, text = explain(conn, variant.query, repetitions)
    return VariantResult(variant, ms, summary, text)


def relation_sizes(conn: psycopg.Connection) -> list[tuple[str, int]]:
    """Tamaño total (todas las particiones) de la tabla y de cada índice."""
    rows = conn.execute(
        """
        WITH parts AS (
            SELECT relid FROM pg_partition_tree(%(t)s::regclass) WHERE isleaf
        )
        SELECT 'Tabla (heap)', sum(pg_relation_size(relid)) FROM parts
        UNION ALL
        SELECT ic.relname, sum(pg_relation_size(t.relid))
        FROM pg_index parent
        JOIN pg_class ic ON ic.oid = parent.indexrelid
        CROSS JOIN LATERAL pg_partition_tree(parent.indexrelid) t
        WHERE parent.indrelid = %(t)s::regclass AND t.isleaf
        GROUP BY ic.relname
        """,
        {"t": QUALIFIED},
    ).fetchall()
    return [(name, int(size or 0)) for name, size in rows]


def btree_equivalent_size(conn: psycopg.Connection) -> int:
    """Tamaño que tendría un B-tree sobre event_ts (se crea y se deshace)."""
    with conn.transaction(force_rollback=True):
        conn.execute(f"CREATE INDEX bench_event_btree ON {QUALIFIED} (event_ts)")
        return int(
            conn.execute(
                """
                SELECT sum(pg_relation_size(relid))
                FROM pg_partition_tree(%s::regclass) WHERE isleaf
                """,
                (f"{SCHEMA}.bench_event_btree",),
            ).fetchone()[0]
        )


def environment(conn: psycopg.Connection) -> dict[str, str]:
    pg, postgis, shared_buffers, work_mem = conn.execute(
        """
        SELECT current_setting('server_version'), postgis_lib_version(),
               current_setting('shared_buffers'), current_setting('work_mem')
        """
    ).fetchone()
    return {
        "PostgreSQL": pg,
        "PostGIS": postgis,
        "shared_buffers": shared_buffers,
        "work_mem": work_mem,
        "CPU (cliente)": f"{os.cpu_count()} núcleos lógicos",
        "SO (cliente)": f"{platform.system()} {platform.release()}",
        "Fecha": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


def run(
    conn: psycopg.Connection,
    cfg: BenchmarkConfig,
    *,
    reuse: bool = False,
    log: Callable[[str], None] = print,
) -> BenchmarkResult:
    """``conn`` debe ser autocommit y del dueño del esquema (rol OWNER)."""
    if not conn.autocommit:
        raise ValueError("El benchmark necesita una conexión autocommit (VACUUM)")
    exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (QUALIFIED,)).fetchone()[0]
    load_s = 0.0
    if not (reuse and exists):
        log(f"Generando {cfg.expected_rows:,} filas en {QUALIFIED}…")
        prepare_schema(conn, cfg)
        load_s = load_data(conn, cfg, log)
    rows = conn.execute(f"SELECT count(*) FROM {QUALIFIED}").fetchone()[0]

    results: list[tuple[Scenario, list[VariantResult]]] = []
    for scenario in scenarios(cfg):
        log(f"Midiendo: {scenario.title}")
        variant_results = [run_variant(conn, v, cfg.repetitions) for v in scenario.variants]
        for vr in variant_results:
            log(f"  {vr.variant.name}: {vr.median_ms:.1f} ms")
        results.append((scenario, variant_results))

    return BenchmarkResult(
        config=cfg,
        rows=rows,
        load_s=load_s,
        environment=environment(conn),
        sizes=relation_sizes(conn),
        btree_equivalent_bytes=btree_equivalent_size(conn),
        scenarios=results,
    )


# -------------------------------------------------------------------- informe
def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:,.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:,.0f} KB"


def _ms(x: float) -> str:
    return f"{x:,.1f} ms" if x < 10_000 else f"{x / 1000:,.1f} s"


def render_markdown(result: BenchmarkResult) -> str:
    cfg = result.config
    lines: list[str] = [
        "# Benchmark · consultas espaciales sobre millones de puntos GPS",
        "",
        "> Generado con `python -m simon_geo.benchmark`. Los números dependen de la máquina;",
        "> lo relevante es la **diferencia entre variantes** y el **plan** que elige PostgreSQL.",
        "",
        "## Datos",
        "",
        f"* **{result.rows:,} puntos**: {cfg.vehicles} vehículos × {cfg.days} días × "
        f"1 reporte cada {cfg.interval_s} s, en Bogotá.",
        f"* Tabla `{QUALIFIED}`: misma definición, particiones diarias e índices que "
        "`bronze.telemetry_raw`.",
    ]
    if result.load_s:
        rate = result.rows / result.load_s
        lines.append(
            f"* Carga (`INSERT … SELECT`, con índices activos): {result.load_s:,.1f} s "
            f"→ **{rate:,.0f} filas/s**."
        )
    lines += ["", "| Entorno | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in result.environment.items()]

    lines += [
        "",
        "## Resumen",
        "",
        "| Consulta | Variante | Tiempo (mediana) | Páginas leídas | Particiones | Acceso |",
        "|---|---|---:|---:|---:|---|",
    ]
    for scenario, variants in result.scenarios:
        for i, vr in enumerate(variants):
            s = vr.summary
            lines.append(
                f"| {scenario.title if i == 0 else ''} | {vr.variant.name} | "
                f"{_ms(vr.median_ms)} | {s.buffers:,} | {s.partitions_scanned} | "
                f"{'<br>'.join(s.access_paths)} |"
            )

    lines += ["", "## Tamaño de la tabla y sus índices", "", "| Objeto | Tamaño |", "|---|---:|"]
    lines += [f"| `{name}` | {_mb(size)} |" for name, size in result.sizes]
    lines.append(
        f"| *B-tree equivalente sobre `event_ts` (creado y descartado)* | "
        f"{_mb(result.btree_equivalent_bytes)} |"
    )

    lines += ["", "## Planes de ejecución", ""]
    for scenario, variants in result.scenarios:
        lines += [f"### {scenario.title}", "", scenario.question, ""]
        lines += ["```sql", variants[0].variant.query.strip(), "```", ""]
        for vr in variants:
            lines += [
                "<details>",
                f"<summary>{vr.variant.name} · {_ms(vr.median_ms)}</summary>",
                "",
                "```",
                vr.text_plan,
                "```",
                "</details>",
                "",
            ]
    return "\n".join(lines) + "\n"


def as_dict(result: BenchmarkResult) -> dict[str, Any]:
    """Resultado en JSON (para comparar ejecuciones)."""
    return {
        "rows": result.rows,
        "load_s": result.load_s,
        "environment": result.environment,
        "sizes": dict(result.sizes),
        "btree_equivalent_bytes": result.btree_equivalent_bytes,
        "scenarios": {
            scenario.key: {
                vr.variant.name: {
                    "median_ms": vr.median_ms,
                    "buffers": vr.summary.buffers,
                    "partitions_scanned": vr.summary.partitions_scanned,
                    "access_paths": list(vr.summary.access_paths),
                }
                for vr in variants
            }
            for scenario, variants in result.scenarios
        },
    }
