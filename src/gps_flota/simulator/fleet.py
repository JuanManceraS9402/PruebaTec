"""Simulador de flota sobre rutas planificadas.

Cada vehículo avanza sobre su ruta en EPSG:9377 (metros) con una dinámica simple:
acelera/frena hacia una velocidad objetivo = límite de la zona × "cumplimiento"
del conductor. Inyecta las imperfecciones reales de la telemetría AVL:

* ruido del sensor de combustible,
* saltos de GPS (outliers),
* retransmisión de paquetes (duplicados),
* frenadas bruscas,
* tanqueos y vehículos que se quedan sin combustible.

Es determinista para una semilla dada (reproducible en tests y en el video).
"""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from gps_flota.domain import TelemetryPoint, Vehicle
from gps_flota.parameters import PipelineParameters
from gps_flota.spatial.crs import lonlat_to_metric, metric_to_lonlat
from gps_flota.spatial.geofencing import GeofenceIndex, resolve_speed_limit
from gps_flota.spatial.routes import PlannedRoute


@dataclass(frozen=True, slots=True)
class DriverProfile:
    """Forma de conducir de un perfil simulado.

    La velocidad objetivo es ``límite × compliance``, con dos matices realistas:

    * ``low_limit_floor_kmh``: en zonas de límite bajo (escolar, patio) el conductor
      no baja de esta velocidad, es decir, no reduce lo suficiente.
    * Rachas: con probabilidad ``burst_prob`` por reporte empieza un tramo de
      ``burst_s`` segundos a ``límite × burst_factor`` (adelantamientos, vía libre).
    """

    name: str
    compliance: float  # velocidad objetivo = límite × compliance
    max_accel_mps2: float  # aceleración máxima habitual
    harsh_brake_prob: float  # probabilidad por reporte de frenada brusca (a más de 50 km/h)
    consumption_factor: float  # conducción agresiva consume más
    low_limit_floor_kmh: float = 0.0
    burst_prob: float = 0.0
    burst_factor: float = 1.0
    burst_s: tuple[float, float] = (20.0, 40.0)


DRIVER_PROFILES = MappingProxyType(
    {
        "compliant": DriverProfile("compliant", 0.92, 1.2, 0.000, 1.00),
        # Respeta el límite casi siempre, pero tiene rachas de exceso y no reduce lo
        # suficiente en zonas de 30 km/h o menos.
        "moderate": DriverProfile(
            "moderate",
            0.97,
            1.8,
            0.002,
            1.08,
            low_limit_floor_kmh=36.0,
            burst_prob=0.006,
            burst_factor=1.2,
        ),
        "aggressive": DriverProfile("aggressive", 1.35, 3.4, 0.012, 1.20),
    }
)

COMFORT_BRAKE_MPS2 = 2.0
STOP_STEPS_REFUEL = 4
STOP_STEPS_STRANDED = 5


@dataclass(frozen=True, slots=True)
class SimulatedVehicle:
    vehicle: Vehicle
    route: PlannedRoute
    driver: DriverProfile
    initial_fuel_pct: float
    start_ts: datetime
    refuel_at_km: float | None = None


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    interval_s: float = 3.0
    outlier_prob: float = 0.004
    duplicate_prob: float = 0.01
    fuel_noise_pct: float = 0.15
    seed: int = 42


class FleetSimulator:
    def __init__(
        self,
        fleet: Sequence[SimulatedVehicle],
        geofence_index: GeofenceIndex,
        params: PipelineParameters | None = None,
        config: SimulationConfig | None = None,
    ) -> None:
        self._fleet = tuple(fleet)
        self._index = geofence_index
        self._params = params or PipelineParameters()
        self._config = config or SimulationConfig()

    def run(self) -> Iterator[TelemetryPoint]:
        """Flujo de paquetes de toda la flota, intercalados por timestamp."""
        streams = [
            self._simulate(sv, random.Random(f"{self._config.seed}:{sv.vehicle.vehicle_id}"))
            for sv in self._fleet
        ]
        return heapq.merge(*streams, key=lambda p: p.event_ts)

    # ------------------------------------------------------------------ interno
    def _simulate(self, sv: SimulatedVehicle, rng: random.Random) -> Iterator[TelemetryPoint]:
        cfg = self._config
        dt = cfg.interval_s
        tank = sv.vehicle.fuel_tank_capacity_l
        consumption = sv.vehicle.nominal_consumption_l_100km * sv.driver.consumption_factor
        length_m = sv.route.metric_geometry.length

        s_m = 0.0
        v_kmh = 0.0
        fuel_l = sv.initial_fuel_pct / 100 * tank
        ts = sv.start_ts
        refueled = False
        burst_until = ts

        def emit(speed: float) -> Iterator[TelemetryPoint]:
            lon, lat = sv.route.point_at_m(s_m)
            if rng.random() < cfg.outlier_prob:
                lon, lat = self._jump(lon, lat, rng)
            fuel_pct = fuel_l / tank * 100 + rng.gauss(0, cfg.fuel_noise_pct)
            point = TelemetryPoint(
                vehicle_id=sv.vehicle.vehicle_id,
                event_ts=ts,
                lat=round(lat, 6),
                lon=round(lon, 6),
                speed_kmh=round(max(speed, 0.0), 1),
                fuel_level_pct=round(min(max(fuel_pct, 0.0), 100.0), 1),
            )
            yield point
            if rng.random() < cfg.duplicate_prob:
                yield point  # retransmisión del equipo AVL

        def brake_to_stop() -> Iterator[TelemetryPoint]:
            """Frenado de confort hasta 0 (sin generar falsos eventos bruscos)."""
            nonlocal v_kmh, s_m, ts
            step = COMFORT_BRAKE_MPS2 * dt * 3.6
            while v_kmh > 0:
                v_kmh = max(v_kmh - step, 0.0)
                s_m = min(s_m + v_kmh / 3.6 * dt, length_m)
                yield from emit(v_kmh)
                ts += timedelta(seconds=dt)

        while True:
            lon, lat = sv.route.point_at_m(s_m)
            decision = resolve_speed_limit(
                self._index.containing(lon, lat), self._params.default_speed_limit_kmh
            )
            driver = sv.driver
            if ts >= burst_until and rng.random() < driver.burst_prob:
                burst_until = ts + timedelta(seconds=rng.uniform(*driver.burst_s))
            factor = driver.burst_factor if ts < burst_until else driver.compliance
            limit = decision.limit_kmh
            target = max(limit * factor, driver.low_limit_floor_kmh) * rng.uniform(0.95, 1.05)

            if v_kmh > 50 and rng.random() < sv.driver.harsh_brake_prob:
                dv = -rng.uniform(3.8, 5.0) * dt * 3.6
            else:
                max_up = sv.driver.max_accel_mps2 * dt * 3.6
                max_down = COMFORT_BRAKE_MPS2 * dt * 3.6
                dv = min(max(target - v_kmh, -max_down), max_up)
            v_kmh = max(v_kmh + dv, 0.0)

            yield from emit(v_kmh)

            ds = v_kmh / 3.6 * dt
            s_m = min(s_m + ds, length_m)
            fuel_l = max(fuel_l - ds / 1000 * consumption / 100, 0.0)
            ts += timedelta(seconds=dt)

            if fuel_l <= 0:  # se queda varado
                yield from brake_to_stop()
                for _ in range(STOP_STEPS_STRANDED):
                    yield from emit(0.0)
                    ts += timedelta(seconds=dt)
                return

            if sv.refuel_at_km is not None and not refueled and s_m / 1000 >= sv.refuel_at_km:
                yield from brake_to_stop()
                for step in range(STOP_STEPS_REFUEL):
                    if step == STOP_STEPS_REFUEL // 2:
                        fuel_l = 0.95 * tank
                    yield from emit(0.0)
                    ts += timedelta(seconds=dt)
                refueled = True

            if s_m >= length_m:
                yield from brake_to_stop()
                yield from emit(0.0)
                return

    @staticmethod
    def _jump(lon: float, lat: float, rng: random.Random) -> tuple[float, float]:
        """Desplaza la posición 1,5–4 km en una dirección aleatoria (salto de GPS)."""
        x, y = lonlat_to_metric(lon, lat)
        dist = rng.uniform(1500, 4000)
        angle = rng.uniform(0, 2 * math.pi)
        return metric_to_lonlat(x + dist * math.cos(angle), y + dist * math.sin(angle))
