"""Exceso de velocidad adaptado a la geocerca."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from simon_geo.domain import Severity, ZoneType
from simon_geo.spatial.geofencing import SpeedLimitDecision

# Exceso (km/h) por encima del límite → severidad base
SEVERITY_BANDS: Final[tuple[tuple[float, Severity], ...]] = (
    (10.0, Severity.LOW),
    (20.0, Severity.MEDIUM),
    (30.0, Severity.HIGH),
)

# En estas zonas el mismo exceso es más grave: la severidad sube un nivel.
ESCALATING_ZONES: Final[frozenset[ZoneType]] = frozenset({ZoneType.SCHOOL, ZoneType.HIGH_RISK})


@dataclass(frozen=True, slots=True)
class SpeedEvaluation:
    limit_kmh: float
    excess_kmh: float  # 0 si no hay exceso
    is_speeding: bool
    severity: Severity | None


def base_severity(excess_kmh: float) -> Severity:
    for upper, severity in SEVERITY_BANDS:
        if excess_kmh < upper:
            return severity
    return Severity.CRITICAL


def evaluate_speed(
    speed_kmh: float, decision: SpeedLimitDecision, tolerance_kmh: float
) -> SpeedEvaluation:
    """Evalúa la velocidad contra el límite aplicable.

    Hay exceso solo si se supera el límite en MÁS de ``tolerance_kmh`` (margen por
    error de medición del GPS). El exceso reportado se mide contra el límite real.
    """
    if speed_kmh < 0:
        raise ValueError("La velocidad no puede ser negativa")
    if tolerance_kmh < 0:
        raise ValueError("La tolerancia no puede ser negativa")

    excess = speed_kmh - decision.limit_kmh
    if excess <= tolerance_kmh:
        return SpeedEvaluation(
            limit_kmh=decision.limit_kmh,
            excess_kmh=max(excess, 0.0),
            is_speeding=False,
            severity=None,
        )

    severity = base_severity(excess)
    if decision.zone_type in ESCALATING_ZONES:
        severity = severity.escalate()
    return SpeedEvaluation(
        limit_kmh=decision.limit_kmh, excess_kmh=excess, is_speeding=True, severity=severity
    )
