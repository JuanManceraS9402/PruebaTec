"""Parámetros de negocio del pipeline.

Espejo tipado de la tabla ``ops.parameter``: los valores por defecto coinciden
con los sembrados en ``db/init/01_schemas_ops.sql`` y en producción se cargan
desde la base con :meth:`PipelineParameters.from_mapping`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Self


@dataclass(frozen=True, slots=True)
class PipelineParameters:
    default_speed_limit_kmh: float = 80.0
    speed_tolerance_kmh: float = 3.0
    gps_max_implied_speed_kmh: float = 200.0
    harsh_accel_mps2: float = 3.0
    harsh_brake_mps2: float = 3.5
    accel_max_gap_s: float = 30.0
    refuel_min_jump_pct: float = 5.0
    fuel_safety_factor: float = 1.15
    consumption_window_km: float = 30.0
    min_consumption_sample_km: float = 3.0

    def __post_init__(self) -> None:
        for f in fields(self):
            if getattr(self, f.name) <= 0:
                raise ValueError(f"El parámetro {f.name} debe ser positivo")
        if self.fuel_safety_factor < 1:
            raise ValueError("fuel_safety_factor debe ser >= 1")

    @classmethod
    def from_mapping(cls, values: Mapping[str, float | Decimal]) -> Self:
        """Construye los parámetros ignorando claves desconocidas (p.ej. filas nuevas en BD)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in values.items() if k in known})
