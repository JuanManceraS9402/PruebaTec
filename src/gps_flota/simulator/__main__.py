"""CLI del simulador.

Ejemplo:
    python -m gps_flota.simulator --out data/telemetry.jsonl --seed 42
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from gps_flota.seed import bogota
from gps_flota.simulator.fleet import FleetSimulator, SimulationConfig
from gps_flota.spatial.geofencing import GeofenceIndex

BOGOTA_TZ = ZoneInfo("America/Bogota")


# La jornada simulada dura menos de una hora; con este margen ya terminó del todo
_SHIFT_DONE_AFTER = timedelta(hours=2)


def _default_start(now: datetime | None = None) -> datetime:
    """La jornada más reciente que ya terminó: hoy a las 06:00 (Bogotá) o, si aún no
    ha terminado, ayer. La API rechaza paquetes con fecha futura, así que simular
    "hoy" de madrugada no cargaría nada."""
    now = now or datetime.now(UTC)
    local_now = now.astimezone(BOGOTA_TZ)
    start = datetime.combine(local_now.date(), time(6, 0), tzinfo=BOGOTA_TZ)
    if local_now < start + _SHIFT_DONE_AFTER:
        start -= timedelta(days=1)
    return start.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulador de flota AVL (escenario Bogotá)")
    parser.add_argument("--out", type=Path, required=True, help="Archivo JSONL de salida")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--interval", type=float, default=3.0, help="Segundos entre reportes")
    parser.add_argument(
        "--start",
        type=datetime.fromisoformat,
        default=None,
        help="Inicio ISO-8601 con zona (por defecto hoy 06:00 America/Bogota)",
    )
    parser.add_argument("--days", type=int, default=1, help="Repetir el escenario N días")
    parser.add_argument(
        "--api-url",
        default=None,
        help="Además de escribir el archivo, enviar a la API (p.ej. http://localhost:8000)",
    )
    parser.add_argument("--api-key", default="dev-ingest-key")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    start = args.start or _default_start()
    if start.tzinfo is None:
        parser.error("--start debe incluir zona horaria, p.ej. 2026-11-20T06:00:00-05:00")
    start = start.astimezone(UTC)  # el pipeline trabaja en UTC (ver DESIGN.md)

    index = GeofenceIndex(bogota.geofences())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    per_vehicle: Counter[str] = Counter()

    sender = ApiSender(args.api_url, args.api_key, args.batch_size) if args.api_url else None

    with args.out.open("w", encoding="utf-8") as fh:
        for day in range(args.days):
            fleet = bogota.scenario(start + timedelta(days=day))
            config = SimulationConfig(interval_s=args.interval, seed=args.seed + day)
            for point in FleetSimulator(fleet, index, config=config).run():
                record = point.to_dict()
                fh.write(json.dumps(record) + "\n")
                per_vehicle[point.vehicle_id] += 1
                if sender:
                    sender.add(record)
    if sender:
        sender.flush()

    total = sum(per_vehicle.values())
    print(f"{total} paquetes escritos en {args.out}")
    for vehicle_id, n in sorted(per_vehicle.items()):
        print(f"  {vehicle_id}: {n}")
    if sender:
        print(f"API: {sender.totals}")
    return 0


class ApiSender:
    """Envía los paquetes a la API en lotes, como lo haría un gateway AVL."""

    def __init__(self, base_url: str, api_key: str, batch_size: int) -> None:
        self._client = httpx.Client(base_url=base_url, headers={"X-API-Key": api_key}, timeout=30)
        self._batch_size = batch_size
        self._buffer: list[dict] = []
        self.totals: Counter[str] = Counter()

    def add(self, record: dict) -> None:
        self._buffer.append(record)
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        response = self._client.post("/v1/telemetry", json={"packets": self._buffer})
        response.raise_for_status()
        body = response.json()
        self.totals.update(
            inserted=body["inserted"],
            duplicates=body["duplicates"],
            rejected=len(body["rejected"]),
        )
        self._buffer.clear()


if __name__ == "__main__":
    sys.exit(main())
