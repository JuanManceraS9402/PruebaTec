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

# ---------------------------------------------------------------------------
# Worker incremental Bronce → Plata (ver processing/worker.py)
# ---------------------------------------------------------------------------

# FOR UPDATE serializa workers concurrentes: el segundo espera a que el primero
# confirme y luego ve el watermark ya avanzado. '-infinity' (nunca procesado) se
# devuelve como NULL: Python no puede representar timestamps infinitos.
LOCK_WATERMARK: Final = """
SELECT CASE WHEN isfinite(last_ingested_at) THEN last_ingested_at END
FROM ops.watermark
WHERE pipeline = %(pipeline)s
FOR UPDATE
"""

# Filas nuevas por vehículo desde el watermark. El límite superior
# (``clock_timestamp() - settle``) deja fuera las transacciones de ingesta que
# podrían seguir en vuelo con un ``ingested_at`` ya asignado. El índice BRIN
# sobre ingested_at descarta los bloques antiguos.
PENDING_VEHICLES: Final = """
SELECT vehicle_id,
       min(event_ts)    AS first_new_ts,
       max(ingested_at) AS max_ingested_at,
       count(*)         AS new_rows
FROM bronze.telemetry_raw
WHERE ingested_at >  coalesce(%(watermark)s::timestamptz, '-infinity')
  AND ingested_at <= clock_timestamp() - make_interval(secs => %(settle_s)s)
GROUP BY vehicle_id
ORDER BY vehicle_id
"""

ADVANCE_WATERMARK: Final = """
UPDATE ops.watermark
SET last_ingested_at = %(ingested_at)s,
    updated_at       = now()
WHERE pipeline = %(pipeline)s
"""

ENSURE_SILVER_PARTITIONS: Final = """
SELECT ops.ensure_daily_partitions('silver.telemetry_enriched', %(from_day)s, %(to_day)s)
"""

# Reprocesar un rango recalcula las filas: DO UPDATE (no DO NOTHING), para que un
# dato tardío corrija los puntos posteriores ya enriquecidos.
UPSERT_SILVER_POINT: Final = """
INSERT INTO silver.telemetry_enriched (
    vehicle_id, event_ts, geom, speed_kmh, fuel_level_pct,
    prev_event_ts, delta_s, segment_distance_m, implied_speed_kmh, accel_mps2,
    fuel_delta_pct, is_refuel, is_gps_outlier, is_harsh_event,
    geofence_ids, applied_speed_limit_kmh, speed_excess_kmh, is_speeding,
    consumption_l_100km, est_range_km, remaining_route_km, is_low_fuel_risk
) VALUES (
    %(vehicle_id)s, %(event_ts)s, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
    %(speed_kmh)s, %(fuel_level_pct)s,
    %(prev_event_ts)s, %(delta_s)s, %(segment_distance_m)s, %(implied_speed_kmh)s,
    %(accel_mps2)s, %(fuel_delta_pct)s, %(is_refuel)s, %(is_gps_outlier)s, %(is_harsh_event)s,
    %(geofence_ids)s, %(applied_speed_limit_kmh)s, %(speed_excess_kmh)s, %(is_speeding)s,
    %(consumption_l_100km)s, %(est_range_km)s, %(remaining_route_km)s, %(is_low_fuel_risk)s
)
ON CONFLICT (vehicle_id, event_ts) DO UPDATE SET
    geom                    = EXCLUDED.geom,
    speed_kmh               = EXCLUDED.speed_kmh,
    fuel_level_pct          = EXCLUDED.fuel_level_pct,
    prev_event_ts           = EXCLUDED.prev_event_ts,
    delta_s                 = EXCLUDED.delta_s,
    segment_distance_m      = EXCLUDED.segment_distance_m,
    implied_speed_kmh       = EXCLUDED.implied_speed_kmh,
    accel_mps2              = EXCLUDED.accel_mps2,
    fuel_delta_pct          = EXCLUDED.fuel_delta_pct,
    is_refuel               = EXCLUDED.is_refuel,
    is_gps_outlier          = EXCLUDED.is_gps_outlier,
    is_harsh_event          = EXCLUDED.is_harsh_event,
    geofence_ids            = EXCLUDED.geofence_ids,
    applied_speed_limit_kmh = EXCLUDED.applied_speed_limit_kmh,
    speed_excess_kmh        = EXCLUDED.speed_excess_kmh,
    is_speeding             = EXCLUDED.is_speeding,
    consumption_l_100km     = EXCLUDED.consumption_l_100km,
    est_range_km            = EXCLUDED.est_range_km,
    remaining_route_km      = EXCLUDED.remaining_route_km,
    is_low_fuel_risk        = EXCLUDED.is_low_fuel_risk,
    processed_at            = now()
"""

# Las alertas del rango reprocesado se recalculan completas: borrar y reinsertar
# evita que queden alertas "huérfanas" si un dato tardío cambia los episodios.
DELETE_ALERTS_FROM: Final = """
DELETE FROM silver.alert
WHERE vehicle_id = %(vehicle_id)s
  AND event_ts  >= %(from_ts)s
"""

INSERT_ALERT: Final = """
INSERT INTO silver.alert (
    vehicle_id, event_ts, alert_type, severity, geofence_id, geom,
    observed_value, threshold_value, details
) VALUES (
    %(vehicle_id)s, %(event_ts)s, %(alert_type)s::silver.alert_type,
    %(severity)s::silver.alert_severity, %(geofence_id)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
    %(observed_value)s, %(threshold_value)s, %(details)s
)
ON CONFLICT (vehicle_id, event_ts, alert_type) DO NOTHING
"""

REFRESH_GOLD: Final = "SELECT gold.refresh_all(%(concurrently)s)"
