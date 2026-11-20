"""Consultas SQL-espaciales del pipeline.

Pertenencia a geocercas en lote
-------------------------------
Para cada punto de Bronce, un ``LEFT JOIN LATERAL`` contra ``ref.geofence``
con ``ST_Intersects``:

1. El operador ``&&`` implícito en ``ST_Intersects`` usa el índice GiST
   ``geofence_geom_gist`` (parcial, solo geocercas activas) para descartar
   por bounding box todas las geocercas que no pueden contener el punto.
2. Solo sobre los candidatos se ejecuta el test exacto punto-en-polígono.
3. ``array_agg`` devuelve todas las geocercas del punto (pueden solaparse);
   la regla de prioridad entre ellas se aplica en Python
   (``spatial.geofencing.resolve_speed_limit``), donde está probada.

Se usa ``ST_Intersects`` y no ``ST_Contains`` para que un punto sobre el borde
cuente como dentro (misma semántica que el índice en memoria).
"""

from __future__ import annotations

from typing import Final

BRONZE_POINTS_WITH_GEOFENCES: Final = """
SELECT
    b.vehicle_id,
    b.event_ts,
    b.lat,
    b.lon,
    b.speed_kmh,
    b.fuel_level_pct,
    coalesce(gf.geofence_ids, '{}'::integer[]) AS geofence_ids
FROM bronze.telemetry_raw AS b
LEFT JOIN LATERAL (
    SELECT array_agg(g.geofence_id ORDER BY g.geofence_id) AS geofence_ids
    FROM ref.geofence AS g
    WHERE g.active
      AND ST_Intersects(g.geom, b.geom)
) AS gf ON true
WHERE b.vehicle_id = ANY(%(vehicle_ids)s)
  AND b.event_ts >= %(from_ts)s
  AND b.event_ts <  %(to_ts)s
ORDER BY b.vehicle_id, b.event_ts
"""
