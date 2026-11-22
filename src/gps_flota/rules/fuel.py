"""Consumo predictivo de combustible.

Algoritmo
---------
1. **Consumo real** (L/100 km): regresión lineal de nivel de combustible (%) vs
   odómetro (km) en una ventana móvil de ``window_km``. La pendiente por mínimos
   cuadrados es robusta al ruido del sensor de nivel (que oscila ±0,1–0,3 %),
   a diferencia de restar el primer y el último valor.
2. Mientras no hay suficiente recorrido en la ventana, o si la estimación es
   físicamente implausible, se usa el **consumo nominal** del vehículo.
3. Un tanqueo rompe la serie: la ventana se reinicia.
4. **Autonomía** = litros actuales / consumo.
5. **Riesgo**: si la autonomía no cubre la distancia restante de la ruta
   multiplicada por un factor de seguridad.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from gps_flota.domain import Severity

# Rango plausible del consumo medido respecto al nominal; fuera de él se asume
# ruido del sensor (p.ej. pendiente casi nula en un trayecto cuesta abajo corto).
PLAUSIBLE_RATIO: Final = (0.25, 4.0)
MIN_SAMPLES: Final = 3


class ConsumptionSource(StrEnum):
    MEASURED = "MEASURED"
    NOMINAL = "NOMINAL"


@dataclass(frozen=True, slots=True)
class ConsumptionEstimate:
    l_100km: float
    source: ConsumptionSource


@dataclass(frozen=True, slots=True)
class FuelRiskAssessment:
    range_km: float
    remaining_km: float
    required_km: float  # remaining_km × factor de seguridad
    is_risk: bool
    severity: Severity | None


def is_refuel(prev_pct: float, curr_pct: float, min_jump_pct: float) -> bool:
    """Un salto hacia arriba mayor que ``min_jump_pct`` es un tanqueo (no ruido)."""
    return curr_pct - prev_pct >= min_jump_pct


class ConsumptionEstimator:
    """Estimador incremental de consumo por vehículo."""

    def __init__(
        self,
        tank_capacity_l: float,
        nominal_l_100km: float,
        window_km: float,
        min_sample_km: float,
    ) -> None:
        if tank_capacity_l <= 0 or nominal_l_100km <= 0:
            raise ValueError("Capacidad y consumo nominal deben ser positivos")
        if min_sample_km > window_km:
            raise ValueError("min_sample_km no puede superar window_km")
        self._tank = tank_capacity_l
        self._nominal = nominal_l_100km
        self._window_km = window_km
        self._min_sample_km = min_sample_km
        self._samples: deque[tuple[float, float]] = deque()  # (odómetro km, combustible %)

    def reset(self) -> None:
        self._samples.clear()

    def add(self, odometer_km: float, fuel_pct: float) -> None:
        if self._samples and odometer_km < self._samples[-1][0]:
            raise ValueError("El odómetro no puede retroceder")
        self._samples.append((odometer_km, fuel_pct))
        while len(self._samples) > MIN_SAMPLES and (
            self._samples[-1][0] - self._samples[1][0] >= self._window_km
        ):
            self._samples.popleft()

    @property
    def span_km(self) -> float:
        return self._samples[-1][0] - self._samples[0][0] if self._samples else 0.0

    def estimate(self) -> ConsumptionEstimate:
        nominal = ConsumptionEstimate(self._nominal, ConsumptionSource.NOMINAL)
        if len(self._samples) < MIN_SAMPLES or self.span_km < self._min_sample_km:
            return nominal

        slope_pct_per_km = _least_squares_slope(self._samples)
        if slope_pct_per_km is None:
            return nominal
        # %/km → L/km = -pendiente/100 × tanque ; × 100 → L/100 km
        measured = -slope_pct_per_km * self._tank

        low, high = PLAUSIBLE_RATIO
        if not low * self._nominal <= measured <= high * self._nominal:
            return nominal
        return ConsumptionEstimate(measured, ConsumptionSource.MEASURED)


def _least_squares_slope(samples: deque[tuple[float, float]]) -> float | None:
    n = len(samples)
    mean_x = sum(x for x, _ in samples) / n
    mean_y = sum(y for _, y in samples) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in samples)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in samples)
    return sxy / sxx


def estimate_range_km(fuel_pct: float, tank_capacity_l: float, consumption_l_100km: float) -> float:
    """Kilómetros que se pueden recorrer con el combustible actual."""
    if consumption_l_100km <= 0:
        raise ValueError("El consumo debe ser positivo")
    liters = fuel_pct / 100 * tank_capacity_l
    return liters / consumption_l_100km * 100


def assess_fuel_risk(
    range_km: float, remaining_km: float, safety_factor: float
) -> FuelRiskAssessment:
    """Clasifica el riesgo de no completar la ruta.

    * CRITICAL: la autonomía ni siquiera cubre la distancia restante.
    * HIGH: la cubre, pero sin el margen de seguridad.
    """
    if safety_factor < 1:
        raise ValueError("El factor de seguridad debe ser >= 1")
    required = remaining_km * safety_factor
    if range_km < remaining_km:
        severity: Severity | None = Severity.CRITICAL
    elif range_km < required:
        severity = Severity.HIGH
    else:
        severity = None
    return FuelRiskAssessment(
        range_km=range_km,
        remaining_km=remaining_km,
        required_km=required,
        is_risk=severity is not None,
        severity=severity,
    )
