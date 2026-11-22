"""Carga el escenario de Bogotá en la base de datos.

python -m gps_flota.seed
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from gps_flota.config import DbRole
from gps_flota.db import connect
from gps_flota.reference import upsert_reference
from gps_flota.seed import bogota


def main() -> int:
    assignments = {
        sv.vehicle.vehicle_id: sv.route.code for sv in bogota.scenario(datetime.now(UTC))
    }
    with connect(DbRole.OWNER) as conn:
        ids = upsert_reference(
            conn,
            vehicles=bogota.vehicles(),
            geofences=bogota.geofences(),
            routes=bogota.routes(),
            assignments=assignments,
        )
    print(
        f"Cargados {len(bogota.vehicles())} vehículos, {len(ids)} geocercas, "
        f"{len(bogota.routes())} rutas y {len(assignments)} asignaciones."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
