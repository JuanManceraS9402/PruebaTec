"""Worker incremental Bronce → Plata → Oro.

Cada ejecución (``run_once``) hace, en UNA transacción:

1. Bloquea el watermark ``bronze_to_silver`` (``SELECT ... FOR UPDATE``): dos
   workers no procesan el mismo lote.
2. Busca, por vehículo, las filas de Bronce con ``ingested_at`` posterior al
   watermark. Se usa ``ingested_at`` y no ``event_ts`` para no perder paquetes
   que llegan tarde (equipos que vacían su buffer al recuperar señal).
3. Para cada vehículo relee Bronce desde ``primer event_ts nuevo − lookback``
   con las geocercas resueltas en PostGIS (``ST_Intersects`` + GiST). Los puntos
   del lookback solo "calientan" el estado del :class:`TrackProcessor`
   (odómetro, ventana de consumo, episodio de exceso en curso); no se escriben.
   Sin ese calentamiento un episodio de exceso que cruza dos lotes generaría una
   alerta repetida al inicio del segundo lote.
4. Escribe Plata desde el primer ``event_ts`` nuevo en adelante (upsert de
   puntos; borrado y reinserción de alertas del rango).
5. Avanza el watermark y confirma. Si algo falla, rollback: el lote se
   reintenta completo en la siguiente ejecución (todas las escrituras son
   idempotentes).

Después, fuera de esa transacción, refresca las vistas materializadas de Oro.

La lógica de enriquecimiento está en :func:`enrich_window`, sin E/S, para
probarla sin base de datos.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from gps_flota.domain import Alert, TelemetryPoint
from gps_flota.parameters import PipelineParameters
from gps_flota.processing import queries
from gps_flota.processing.enrichment import EnrichmentResult, SilverEnricher
from gps_flota.reference import ReferenceData, load_parameters, load_reference
from gps_flota.rules.track import EnrichedPoint

log = logging.getLogger(__name__)

PIPELINE: Final = "bronze_to_silver"
# Sin límite superior: también se reprocesan los puntos posteriores ya enriquecidos,
# porque un dato tardío cambia el estado de la traza a partir de él.
_OPEN_END: Final = datetime(9999, 1, 1, tzinfo=UTC)
# ops.ensure_daily_partitions rechaza rangos mayores (protección contra fechas basura).
_MAX_PARTITION_SPAN_DAYS: Final = 366


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    lookback: timedelta = timedelta(hours=2)
    """Historia previa que se relee para continuar el estado de cada traza."""
    settle: timedelta = timedelta(seconds=5)
    """Margen para no leer filas de transacciones de ingesta aún en vuelo."""
    refresh_gold: bool = True

    def __post_init__(self) -> None:
        if self.lookback < timedelta(0) or self.settle < timedelta(0):
            raise ValueError("lookback y settle no pueden ser negativos")


@dataclass(frozen=True, slots=True)
class PendingVehicle:
    vehicle_id: str
    first_new_ts: datetime
    max_ingested_at: datetime
    new_rows: int


@dataclass(slots=True)
class WorkerReport:
    vehicles: int = 0
    new_rows: int = 0
    points_read: int = 0
    points_written: int = 0
    alerts_written: int = 0
    unknown_vehicle_points: int = 0
    watermark: datetime | None = None
    gold_refreshed: bool = False
    elapsed_s: float = 0.0
    unknown_vehicles: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["watermark"] = self.watermark.isoformat() if self.watermark else None
        return out


# --------------------------------------------------------------------- lógica
def enrich_window(
    enricher: SilverEnricher,
    points: Iterable[TelemetryPoint],
    geofences_by_point: Mapping[tuple[str, datetime], Sequence[int]] | None,
    emit_from: datetime,
) -> EnrichmentResult:
    """Enriquece la ventana completa (lookback + datos nuevos) y devuelve solo lo
    que ocurre desde ``emit_from``: los puntos anteriores únicamente aportan estado.

    ``enricher`` debe ser nuevo para la ventana (sin estado previo del vehículo).
    """
    full = enricher.enrich(points, geofences_by_point)
    return EnrichmentResult(
        points=[p for p in full.points if p.event_ts >= emit_from],
        alerts=[a for a in full.alerts if a.event_ts >= emit_from],
        duplicates=full.duplicates,
        unknown_vehicle=[p for p in full.unknown_vehicle if p.event_ts >= emit_from],
    )


def silver_row(p: EnrichedPoint) -> dict[str, Any]:
    """Parámetros de ``UPSERT_SILVER_POINT`` para un punto enriquecido."""
    return {
        "vehicle_id": p.vehicle_id,
        "event_ts": p.event_ts,
        "lon": p.lon,
        "lat": p.lat,
        "speed_kmh": p.speed_kmh,
        "fuel_level_pct": p.fuel_level_pct,
        "prev_event_ts": p.prev_event_ts,
        "delta_s": p.delta_s,
        "segment_distance_m": p.segment_distance_m,
        "implied_speed_kmh": p.implied_speed_kmh,
        "accel_mps2": p.accel_mps2,
        "fuel_delta_pct": p.fuel_delta_pct,
        "is_refuel": p.is_refuel,
        "is_gps_outlier": p.is_gps_outlier,
        "is_harsh_event": p.is_harsh_event,
        "geofence_ids": list(p.geofence_ids),
        "applied_speed_limit_kmh": round(p.applied_speed_limit_kmh),
        "speed_excess_kmh": p.speed_excess_kmh,
        "is_speeding": p.is_speeding,
        "consumption_l_100km": p.consumption_l_100km,
        "est_range_km": p.est_range_km,
        "remaining_route_km": p.remaining_route_km,
        "is_low_fuel_risk": p.is_low_fuel_risk,
    }


def alert_row(a: Alert) -> dict[str, Any]:
    """Parámetros de ``INSERT_ALERT`` para una alerta."""
    return {
        "vehicle_id": a.vehicle_id,
        "event_ts": a.event_ts,
        "alert_type": a.alert_type.value,
        "severity": a.severity.value,
        "geofence_id": a.geofence_id,
        "lon": a.lon,
        "lat": a.lat,
        "observed_value": a.observed_value,
        "threshold_value": a.threshold_value,
        "details": Jsonb(a.details),
    }


# ------------------------------------------------------------------------ E/S
class BronzeToSilverWorker:
    """Procesa incrementalmente lo nuevo de Bronce. La conexión debe ser del rol ETL."""

    def __init__(self, conn: psycopg.Connection, config: WorkerConfig | None = None) -> None:
        if conn.autocommit:
            raise ValueError("El worker necesita una conexión transaccional (autocommit=False)")
        self._conn = conn
        self._config = config or WorkerConfig()

    def run_once(self) -> WorkerReport:
        if self._conn.info.transaction_status != TransactionStatus.IDLE:
            # Dentro de una transacción ajena, transaction() sería un SAVEPOINT y el
            # lote quedaría sin confirmar (y el watermark bloqueado) hasta que el
            # llamador decidiera.
            raise RuntimeError("La conexión del worker tiene una transacción abierta")
        started = time.perf_counter()
        report = WorkerReport()
        with self._conn.transaction():
            watermark = self._lock_watermark()
            pending = self._pending(watermark)
            if pending:
                reference = load_reference(self._conn)
                params = load_parameters(self._conn)
                self._process(pending, reference, params, report)
                report.watermark = max(p.max_ingested_at for p in pending)
                self._conn.execute(
                    queries.ADVANCE_WATERMARK,
                    {"pipeline": PIPELINE, "ingested_at": report.watermark},
                )
            else:
                report.watermark = watermark

        if pending and self._config.refresh_gold:
            # Transacción aparte: el refresco no debe deshacer un lote de Plata ya válido.
            with self._conn.transaction():
                self._conn.execute(queries.REFRESH_GOLD, {"concurrently": True})
            report.gold_refreshed = True

        report.elapsed_s = round(time.perf_counter() - started, 3)
        if report.unknown_vehicles:
            log.warning(
                "%d puntos de vehículos sin maestro (%s): quedan en Bronce sin enriquecer",
                report.unknown_vehicle_points,
                ", ".join(report.unknown_vehicles),
            )
        return report

    # ------------------------------------------------------------ pasos
    def _lock_watermark(self) -> datetime | None:
        """Watermark actual; ``None`` si nunca se ha procesado nada."""
        row = self._conn.execute(queries.LOCK_WATERMARK, {"pipeline": PIPELINE}).fetchone()
        if row is None:
            raise RuntimeError(f"No existe el watermark {PIPELINE!r} en ops.watermark")
        return row[0]

    def _pending(self, watermark: datetime | None) -> list[PendingVehicle]:
        rows = self._conn.execute(
            queries.PENDING_VEHICLES,
            {"watermark": watermark, "settle_s": self._config.settle.total_seconds()},
        ).fetchall()
        return [PendingVehicle(*r) for r in rows]

    def _process(
        self,
        pending: Sequence[PendingVehicle],
        reference: ReferenceData,
        params: PipelineParameters,
        report: WorkerReport,
    ) -> None:
        # Enricher nuevo por ejecución: cada vehículo arranca sin estado y lo
        # reconstruye con los puntos del lookback.
        enricher = SilverEnricher(
            reference.vehicles, reference.geofence_index, params, reference.active_routes
        )
        for vehicle in pending:
            points, geofences = self._read_window(vehicle)
            result = enrich_window(enricher, points, geofences, vehicle.first_new_ts)
            self._write(vehicle, result)

            report.vehicles += 1
            report.new_rows += vehicle.new_rows
            report.points_read += len(points)
            report.points_written += len(result.points)
            report.alerts_written += len(result.alerts)
            if result.unknown_vehicle:
                report.unknown_vehicle_points += len(result.unknown_vehicle)
                report.unknown_vehicles.append(vehicle.vehicle_id)

    def _read_window(
        self, vehicle: PendingVehicle
    ) -> tuple[list[TelemetryPoint], dict[tuple[str, datetime], tuple[int, ...]]]:
        rows = self._conn.execute(
            queries.BRONZE_POINTS_WITH_GEOFENCES,
            {
                "vehicle_ids": [vehicle.vehicle_id],
                "from_ts": vehicle.first_new_ts - self._config.lookback,
                "to_ts": _OPEN_END,
            },
        ).fetchall()
        points: list[TelemetryPoint] = []
        geofences: dict[tuple[str, datetime], tuple[int, ...]] = {}
        for vehicle_id, event_ts, lat, lon, speed, fuel, geofence_ids in rows:
            points.append(TelemetryPoint(vehicle_id, event_ts, lat, lon, speed, fuel))
            geofences[(vehicle_id, event_ts)] = tuple(geofence_ids)
        return points, geofences

    def _write(self, vehicle: PendingVehicle, result: EnrichmentResult) -> None:
        if result.points:
            self._ensure_partitions(result.points)
        with self._conn.cursor() as cur:
            if result.points:
                cur.executemany(queries.UPSERT_SILVER_POINT, [silver_row(p) for p in result.points])
            cur.execute(
                queries.DELETE_ALERTS_FROM,
                {"vehicle_id": vehicle.vehicle_id, "from_ts": vehicle.first_new_ts},
            )
            if result.alerts:
                cur.executemany(queries.INSERT_ALERT, [alert_row(a) for a in result.alerts])

    def _ensure_partitions(self, points: Sequence[EnrichedPoint]) -> None:
        days = [p.event_ts.astimezone(UTC).date() for p in points]
        from_day, to_day = min(days), max(days)
        if (to_day - from_day).days > _MAX_PARTITION_SPAN_DAYS:
            # Fechas anómalas: las filas caen en la partición DEFAULT, que se vigila.
            log.warning("Rango de fechas anómalo %s..%s: no se crean particiones", from_day, to_day)
            return
        self._conn.execute(
            queries.ENSURE_SILVER_PARTITIONS, {"from_day": from_day, "to_day": to_day}
        )
