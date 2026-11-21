"""Ejecuta el benchmark y escribe docs/benchmark.md.

python -m simon_geo.benchmark                         # 5 M de puntos (500 vehículos × 7 días)
python -m simon_geo.benchmark --vehicles 100 --days 2 # prueba rápida
python -m simon_geo.benchmark --reuse                 # re-mide sin regenerar los datos
python -m simon_geo.benchmark --drop                  # borra el esquema bench
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from simon_geo.benchmark.runner import SCHEMA, BenchmarkConfig, as_dict, render_markdown, run
from simon_geo.config import DbRole
from simon_geo.db import connect


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark espacial sobre millones de puntos")
    parser.add_argument("--vehicles", type=int, default=500)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--interval-s", type=int, default=60, help="segundos entre reportes")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--reuse", action="store_true", help="no regenerar si ya hay datos")
    parser.add_argument("--drop", action="store_true", help=f"borrar el esquema {SCHEMA} y salir")
    parser.add_argument("--out", type=Path, default=Path("docs/benchmark.md"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with connect(DbRole.OWNER, autocommit=True) as conn:
            if args.drop:
                conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
                print(f"Esquema {SCHEMA} eliminado.")
                return 0
            cfg = BenchmarkConfig(
                vehicles=args.vehicles,
                days=args.days,
                interval_s=args.interval_s,
                repetitions=args.repetitions,
            )
            result = run(conn, cfg, reuse=args.reuse)
    except psycopg.OperationalError as exc:
        print(f"No se pudo conectar a la base de datos: {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_markdown(result), encoding="utf-8")
    args.out.with_suffix(".json").write_text(
        json.dumps(as_dict(result), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Informe escrito en {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
