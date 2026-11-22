"""Geocercas: pertenencia punto-en-polígono y resolución del límite de velocidad.

La pertenencia usa la misma semántica que ``ST_Intersects`` en PostGIS: un punto
sobre el borde cuenta como dentro (con ``ST_Contains`` quedaría fuera y un
vehículo que circula por el límite de una zona escolar no se evaluaría).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from shapely import STRtree
from shapely.geometry import Point

from gps_flota.domain import Geofence, ZoneType

# Cuando un punto cae en varias geocercas con límite, manda la más específica.
# Ej.: una zona escolar (30) dentro del perímetro urbano (50) → 30;
#      un corredor rápido (60) dentro del perímetro urbano (50) → 60.
# Con igual prioridad se aplica el límite más restrictivo.
ZONE_PRIORITY: Final[Mapping[ZoneType, int]] = MappingProxyType(
    {
        ZoneType.SCHOOL: 60,
        ZoneType.RESTRICTED: 50,
        ZoneType.HIGH_RISK: 40,
        ZoneType.DEPOT: 30,
        ZoneType.HIGHWAY: 20,
        ZoneType.URBAN: 10,
    }
)


@dataclass(frozen=True, slots=True)
class SpeedLimitDecision:
    limit_kmh: float
    governing: Geofence | None  # None → se aplicó el límite por defecto

    @property
    def zone_type(self) -> ZoneType | None:
        return self.governing.zone_type if self.governing else None


class GeofenceIndex:
    """Índice espacial en memoria (R-tree) para evaluar geocercas en tiempo real.

    Equivalente en Python del índice GiST de ``ref.geofence``: filtra por
    bounding box y solo evalúa el predicado exacto sobre los candidatos.
    """

    def __init__(self, geofences: Iterable[Geofence]) -> None:
        self._geofences: tuple[Geofence, ...] = tuple(geofences)
        ids = [g.geofence_id for g in self._geofences]
        if len(ids) != len(set(ids)):
            raise ValueError("Hay geofence_id duplicados")
        self._tree = STRtree([g.geometry for g in self._geofences])
        self._by_id = {g.geofence_id: g for g in self._geofences}

    def __len__(self) -> int:
        return len(self._geofences)

    @property
    def geofences(self) -> tuple[Geofence, ...]:
        return self._geofences

    def containing(self, lon: float, lat: float) -> list[Geofence]:
        """Geocercas que contienen (o tocan) el punto, ordenadas por ID."""
        hits = self._tree.query(Point(lon, lat), predicate="intersects")
        return sorted((self._geofences[i] for i in hits), key=lambda g: g.geofence_id)

    def by_ids(self, geofence_ids: Iterable[int]) -> list[Geofence]:
        """Geocercas ya resueltas en base de datos (ST_Intersects), ordenadas por ID."""
        try:
            return sorted((self._by_id[i] for i in geofence_ids), key=lambda g: g.geofence_id)
        except KeyError as exc:
            raise ValueError(f"Geocerca {exc.args[0]} no está cargada en el índice") from exc


def resolve_speed_limit(
    geofences: Sequence[Geofence], default_limit_kmh: float
) -> SpeedLimitDecision:
    """Límite aplicable a un punto dadas las geocercas que lo contienen."""
    candidates = [g for g in geofences if g.speed_limit_kmh is not None]
    if not candidates:
        return SpeedLimitDecision(limit_kmh=default_limit_kmh, governing=None)

    governing = min(
        candidates,
        # mayor prioridad primero; a igual prioridad, el límite más bajo; luego ID (determinista)
        key=lambda g: (-ZONE_PRIORITY[g.zone_type], g.speed_limit_kmh, g.geofence_id),
    )
    assert governing.speed_limit_kmh is not None
    return SpeedLimitDecision(limit_kmh=float(governing.speed_limit_kmh), governing=governing)
