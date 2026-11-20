"""Lectura y escritura de datos maestros (``ref.*``).

Las geometrías viajan como WKB (binario): sin pérdida de precisión, a
diferencia de WKT, que redondea decimales.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import psycopg
import shapely

from simon_geo.domain import Geofence, Vehicle, ZoneType
from simon_geo.spatial.geofencing import GeofenceIndex
from simon_geo.spatial.routes import PlannedRoute


@dataclass(frozen=True, slots=True)
class ReferenceData:
    vehicles: tuple[Vehicle, ...]
    geofence_index: GeofenceIndex
    active_routes: Mapping[str, PlannedRoute]  # vehicle_id → ruta asignada


# ------------------------------------------------------------------ escritura
def upsert_reference(
    conn: psycopg.Connection,
    *,
    vehicles: Iterable[Vehicle],
    geofences: Iterable[Geofence],
    routes: Iterable[PlannedRoute],
    assignments: Mapping[str, str],
) -> dict[str, int]:
    """Carga idempotente de maestros. Devuelve {código de geocerca: geofence_id en BD}.

    ``assignments``: vehicle_id → código de ruta activa.
    Los IDs de geocercas y rutas los asigna la base; se cruzan por código.
    """
    with conn.cursor() as cur:
        for v in vehicles:
            cur.execute(
                """
                INSERT INTO ref.vehicle
                    (vehicle_id, vehicle_type, fuel_tank_capacity_l, nominal_consumption_l_100km)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (vehicle_id) DO UPDATE SET
                    vehicle_type = EXCLUDED.vehicle_type,
                    fuel_tank_capacity_l = EXCLUDED.fuel_tank_capacity_l,
                    nominal_consumption_l_100km = EXCLUDED.nominal_consumption_l_100km,
                    active = true
                """,
                (
                    v.vehicle_id,
                    v.vehicle_type,
                    v.fuel_tank_capacity_l,
                    v.nominal_consumption_l_100km,
                ),
            )

        geofence_ids: dict[str, int] = {}
        for g in geofences:
            cur.execute(
                """
                INSERT INTO ref.geofence (code, name, zone_type, speed_limit_kmh, geom)
                VALUES (%s, %s, %s::ref.zone_type, %s, ST_Multi(ST_GeomFromWKB(%s, 4326)))
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    zone_type = EXCLUDED.zone_type,
                    speed_limit_kmh = EXCLUDED.speed_limit_kmh,
                    geom = EXCLUDED.geom,
                    active = true
                RETURNING geofence_id
                """,
                (g.code, g.name, g.zone_type.value, g.speed_limit_kmh, g.geometry.wkb),
            )
            geofence_ids[g.code] = cur.fetchone()[0]

        route_ids: dict[str, int] = {}
        for r in routes:
            cur.execute(
                """
                INSERT INTO ref.route (code, name, geom)
                VALUES (%s, %s, ST_GeomFromWKB(%s, 4326))
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, geom = EXCLUDED.geom
                RETURNING route_id
                """,
                (r.code, r.name, r.geometry.wkb),
            )
            route_ids[r.code] = cur.fetchone()[0]

        for vehicle_id, route_code in assignments.items():
            route_id = route_ids[route_code]
            # cerrar la asignación activa si es de otra ruta
            cur.execute(
                """
                UPDATE ref.vehicle_route_assignment SET valid_to = clock_timestamp()
                WHERE vehicle_id = %s AND valid_to IS NULL AND route_id <> %s
                """,
                (vehicle_id, route_id),
            )
            cur.execute(
                """
                INSERT INTO ref.vehicle_route_assignment (vehicle_id, route_id, valid_from)
                SELECT %(v)s, %(r)s, clock_timestamp()
                WHERE NOT EXISTS (
                    SELECT 1 FROM ref.vehicle_route_assignment
                    WHERE vehicle_id = %(v)s AND valid_to IS NULL
                )
                """,
                {"v": vehicle_id, "r": route_id},
            )
    return geofence_ids


# -------------------------------------------------------------------- lectura
def load_reference(conn: psycopg.Connection) -> ReferenceData:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT vehicle_id, vehicle_type, fuel_tank_capacity_l, nominal_consumption_l_100km
            FROM ref.vehicle WHERE active
            """
        )
        vehicles = tuple(
            Vehicle(
                vehicle_id=vid,
                vehicle_type=vtype,
                fuel_tank_capacity_l=float(tank),
                nominal_consumption_l_100km=float(nominal),
            )
            for vid, vtype, tank, nominal in cur.fetchall()
        )

        cur.execute(
            """
            SELECT geofence_id, code, name, zone_type::text, speed_limit_kmh, ST_AsBinary(geom)
            FROM ref.geofence WHERE active
            """
        )
        geofences = [
            Geofence(gid, code, name, ZoneType(zone), limit, shapely.from_wkb(bytes(wkb)))
            for gid, code, name, zone, limit, wkb in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT a.vehicle_id, r.route_id, r.code, r.name, ST_AsBinary(r.geom)
            FROM ref.vehicle_route_assignment a
            JOIN ref.route r USING (route_id)
            WHERE a.valid_to IS NULL
            """
        )
        routes = {
            vid: PlannedRoute(rid, code, name, shapely.from_wkb(bytes(wkb)))
            for vid, rid, code, name, wkb in cur.fetchall()
        }

    return ReferenceData(vehicles, GeofenceIndex(geofences), routes)
