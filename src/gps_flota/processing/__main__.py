"""Ejecuta el worker Bronce → Plata → Oro.

python -m gps_flota.processing                  # un lote y termina
python -m gps_flota.processing --loop -i 10     # cada 10 s hasta Ctrl+C
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import timedelta

import psycopg

from gps_flota.config import DbRole
from gps_flota.db import connect
from gps_flota.processing.worker import BronzeToSilverWorker, WorkerConfig

log = logging.getLogger("gps_flota.processing")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loop", action="store_true", help="procesar de forma continua")
    parser.add_argument(
        "-i", "--interval", type=float, default=10.0, help="segundos entre lotes (--loop)"
    )
    parser.add_argument(
        "--lookback-min",
        type=float,
        default=120.0,
        help="minutos de historia que se releen para continuar el estado de cada traza",
    )
    parser.add_argument(
        "--settle-s",
        type=float,
        default=5.0,
        help="segundos de margen para no leer ingestas aún en vuelo",
    )
    parser.add_argument("--no-gold", action="store_true", help="no refrescar las vistas de Oro")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = WorkerConfig(
        lookback=timedelta(minutes=args.lookback_min),
        settle=timedelta(seconds=args.settle_s),
        refresh_gold=not args.no_gold,
    )
    try:
        with connect(DbRole.ETL) as conn:
            worker = BronzeToSilverWorker(conn, config)
            while True:
                report = worker.run_once()
                log.info("lote: %s", json.dumps(report.as_dict(), ensure_ascii=False))
                if not args.loop:
                    return 0
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except psycopg.OperationalError as exc:
        log.error("No se pudo conectar a la base de datos: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
