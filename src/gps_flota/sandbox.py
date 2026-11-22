"""Simulación bajo demanda: "este vehículo va por esta ruta, ¿qué pasa?".

Recorre el mismo camino que la flota, sin atajos: registra el vehículo, genera su
telemetría, la envía a la API (Bronce) y ejecuta el worker (Plata y Oro). Cada paso
usa su propio rol de base de datos. Si algo falla, el vehículo se borra para no dejar
datos a medias.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
import psycopg

from gps_flota.config import DbRole, Settings
from gps_flota.db import connect
from gps_flota.domain import TelemetryPoint, Vehicle
from gps_flota.processing import queries
from gps_flota.processing.worker import BronzeToSilverWorker, WorkerConfig, WorkerReport
from gps_flota.simulator.fleet import (
    DRIVER_PROFILES,
    FleetSimulator,
    SimulatedVehicle,
    SimulationConfig,
)
from gps_flota.spatial.geofencing import GeofenceIndex
from gps_flota.spatial.routes import PlannedRoute

VEHICLE_TYPE: Final = "SANDBOX"
_BATCH_SIZE: Final = 500
_SETTLE: Final = timedelta(seconds=1)


class SandboxError(Exception):
    """Error explicable al usuario del tablero."""


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    vehicle_id: str
    route_code: str
    profile: str
    fuel_pct: float
    tank_l: float
    consumption_l_100km: float
    start: datetime

    def __post_init__(self) -> None:
        if self.profile not in DRIVER_PROFILES:
            raise SandboxError(f"Perfil desconocido: {self.profile}")
        if not 0 < self.fuel_pct <= 100:
            raise SandboxError("El combustible inicial debe estar entre 0 y 100 %")
        if self.start.tzinfo is None:
            raise SandboxError("La hora de salida debe incluir zona horaria")
        try:
            _ = self.vehicle
        except ValueError as exc:
            raise SandboxError(
                "Datos inválidos: la placa va como VHC-1234-XYZ (3 letras, 4 números, 3 "
                "letras) y el tanque y el consumo deben ser positivos"
            ) from exc

    @property
    def vehicle(self) -> Vehicle:
        return Vehicle(self.vehicle_id, self.tank_l, self.consumption_l_100km, VEHICLE_TYPE)


@dataclass(frozen=True, slots=True)
class SandboxReport:
    vehicle_id: str
    route_code: str
    packets: int
    inserted: int
    points_written: int
    alerts_written: int
    elapsed_s: float


def simulate_points(
    request: SandboxRequest, routes: Iterable[PlannedRoute], index: GeofenceIndex
) -> list[TelemetryPoint]:
    """Telemetría del vehículo, determinista para una misma placa y configuración."""
    route = next((r for r in routes if r.code == request.route_code), None)
    if route is None:
        raise SandboxError(f"La ruta {request.route_code} no existe")
    unit = SimulatedVehicle(
        request.vehicle,
        route,
        DRIVER_PROFILES[request.profile],
        request.fuel_pct,
        request.start.astimezone(UTC),
    )
    return list(FleetSimulator([unit], index, config=SimulationConfig()).run())


def check_time_window(points: Sequence[TelemetryPoint], now: datetime, settings: Settings) -> None:
    """Rechaza antes de escribir lo que la API no aceptaría."""
    if not points:
        raise SandboxError("La simulación no generó telemetría")
    if max(p.event_ts for p in points) > now + timedelta(seconds=settings.ingest_max_future_s):
        raise SandboxError("El recorrido terminaría en el futuro: elige una hora más temprana")
    if min(p.event_ts for p in points) < now - timedelta(days=settings.ingest_max_age_days):
        raise SandboxError(
            f"La API no acepta datos de más de {settings.ingest_max_age_days} días: elige un "
            "día más reciente"
        )


def send_packets(client: httpx.Client, points: Sequence[TelemetryPoint]) -> Counter[str]:
    totals: Counter[str] = Counter()
    for i in range(0, len(points), _BATCH_SIZE):
        batch = [p.to_dict() for p in points[i : i + _BATCH_SIZE]]
        response = client.post("/v1/telemetry", json={"packets": batch})
        response.raise_for_status()
        body = response.json()
        totals.update(
            inserted=body["inserted"],
            duplicates=body["duplicates"],
            rejected=len(body["rejected"]),
        )
    return totals


def purge(settings: Settings, vehicle_id: str | None = None) -> int:
    """Borra uno o todos los vehículos simulados y actualiza Oro."""
    with connect(DbRole.ADMIN, settings, autocommit=True) as conn:
        removed = conn.execute("SELECT ops.sandbox_purge(%s::text)", (vehicle_id,)).fetchone()[0]
    if removed:
        with connect(DbRole.ETL, settings) as conn:
            conn.execute(queries.REFRESH_GOLD, {"concurrently": True})
    return removed


def _process(settings: Settings, vehicle_id: str) -> WorkerReport:
    time.sleep(_SETTLE.total_seconds() + 0.1)  # que lo recién ingerido salga del margen
    with connect(DbRole.ETL, settings) as conn:
        report = BronzeToSilverWorker(conn, WorkerConfig(settle=_SETTLE)).run_once()
        written = conn.execute(
            "SELECT count(*) FROM silver.telemetry_enriched WHERE vehicle_id = %s",
            (vehicle_id,),
        ).fetchone()[0]
        conn.rollback()
    if not written:
        raise SandboxError("El worker no procesó la telemetría del vehículo")
    return report


def _register(
    request: SandboxRequest, settings: Settings, custom_route: PlannedRoute | None
) -> None:
    base = (
        request.vehicle_id,
        request.tank_l,
        request.consumption_l_100km,
        request.route_code,
    )
    try:
        with connect(DbRole.ADMIN, settings, autocommit=True) as conn:
            if custom_route is None:
                conn.execute(
                    "SELECT ops.sandbox_register(%s::text, %s::numeric, %s::numeric, %s::text)",
                    base,
                )
            else:
                conn.execute(
                    "SELECT ops.sandbox_register_with_route("
                    "%s::text, %s::numeric, %s::numeric, %s::text, %s::text, %s::bytea)",
                    (*base, custom_route.name, custom_route.geometry.wkb),
                )
    except psycopg.errors.UniqueViolation as exc:
        raise SandboxError(f"El vehículo {request.vehicle_id} ya existe") from exc
    except psycopg.errors.RaiseException as exc:
        raise SandboxError(exc.diag.message_primary or str(exc)) from exc
    except psycopg.Error as exc:
        raise SandboxError(f"No se pudo registrar el vehículo: {exc}") from exc


def run(
    request: SandboxRequest,
    routes: Iterable[PlannedRoute],
    index: GeofenceIndex,
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    custom_route: PlannedRoute | None = None,
) -> SandboxReport:
    """Registra, simula, ingiere y procesa. Ante un fallo no deja nada a medias.

    ``custom_route``: ruta nueva (ver :mod:`gps_flota.routing`) en lugar de una existente.
    """
    started = time.perf_counter()
    if custom_route is not None:
        routes = [custom_route]
    points = simulate_points(request, routes, index)
    check_time_window(points, now or datetime.now(UTC), settings)

    owns_client = client is None
    client = client or httpx.Client(
        base_url=settings.api_url,
        headers={"X-API-Key": settings.api_key_ingest.get_secret_value()},
        timeout=30,
    )
    try:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError as exc:
            raise SandboxError(
                f"La API no responde en {settings.api_url}. Iníciala con: "
                "uvicorn gps_flota.api.app:app"
            ) from exc

        _register(request, settings, custom_route)
        try:
            totals = send_packets(client, points)
            if totals["rejected"]:
                raise SandboxError(f"La API rechazó {totals['rejected']} paquetes")
            report = _process(settings, request.vehicle_id)
        except (httpx.HTTPError, psycopg.Error, SandboxError) as exc:
            purge(settings, request.vehicle_id)
            if isinstance(exc, SandboxError):
                raise
            raise SandboxError(f"Falló la simulación y se revirtió: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    return SandboxReport(
        vehicle_id=request.vehicle_id,
        route_code=request.route_code,
        packets=len(points),
        inserted=totals["inserted"],
        points_written=report.points_written,
        alerts_written=report.alerts_written,
        elapsed_s=round(time.perf_counter() - started, 1),
    )
