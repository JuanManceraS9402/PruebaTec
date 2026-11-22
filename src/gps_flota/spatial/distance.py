"""Distancias geodésicas."""

from __future__ import annotations

import math
from typing import Final

# Radio medio terrestre (IUGG). Error < 0,5 % frente al elipsoide: suficiente para
# segmentos cortos entre reportes AVL. En base de datos se usa geography (elipsoide).
EARTH_RADIUS_M: Final = 6_371_008.8


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Distancia de gran círculo en metros entre dos puntos (lon, lat) en grados."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
