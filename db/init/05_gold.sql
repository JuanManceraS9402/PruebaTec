-- =====================================================================
-- 05 · Capa Oro: modelos consolidados para BI
-- =====================================================================
-- Vistas materializadas con índice único (permite REFRESH CONCURRENTLY,
-- sin bloquear lecturas del tablero) + índices GiST para filtros por mapa.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Driver Score (0–100). Fórmula en un único lugar, documentada en DESIGN.md.
--   - hasta 50 pts por % del tiempo en exceso de velocidad
--   - hasta 30 pts por eventos bruscos cada 100 km
--   - hasta 20 pts por el peor exceso del día
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gold.driver_score(
  p_speeding_time_pct numeric,
  p_harsh_per_100km   numeric,
  p_max_excess_kmh    numeric
)
RETURNS numeric
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT round(greatest(0,
           100
           - least(50, coalesce(p_speeding_time_pct, 0))
           - least(30, coalesce(p_harsh_per_100km, 0) * 5)
           - least(20, greatest(coalesce(p_max_excess_kmh, 0), 0) * 0.5)
         ), 1)
$$;

-- ---------------------------------------------------------------------
-- KPIs diarios por vehículo (día en hora local de Colombia)
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW gold.vehicle_daily_kpi AS
WITH daily AS (
  SELECT
    t.vehicle_id,
    (t.event_ts AT TIME ZONE 'America/Bogota')::date                       AS local_day,
    count(*)                                                               AS points,
    min(t.event_ts)                                                        AS first_event_ts,
    max(t.event_ts)                                                        AS last_event_ts,
    sum(t.segment_distance_m) FILTER (WHERE NOT t.is_gps_outlier) / 1000.0 AS distance_km,
    sum(t.delta_s)            FILTER (WHERE NOT t.is_gps_outlier)          AS tracked_s,
    sum(t.delta_s)            FILTER (WHERE t.is_speeding)                 AS speeding_s,
    avg(t.speed_kmh)          FILTER (WHERE t.speed_kmh > 0)               AS avg_moving_speed_kmh,
    max(t.speed_kmh)                                                       AS max_speed_kmh,
    count(*)                  FILTER (WHERE t.is_speeding)                 AS speeding_points,
    max(t.speed_excess_kmh)                                                AS max_excess_kmh,
    count(*)                  FILTER (WHERE t.is_harsh_event)              AS harsh_events,
    count(*)                  FILTER (WHERE t.is_low_fuel_risk)            AS low_fuel_risk_points,
    count(*)                  FILTER (WHERE t.is_refuel)                   AS refuels,
    -sum(t.fuel_delta_pct)    FILTER (WHERE t.fuel_delta_pct < 0 AND NOT t.is_refuel) AS fuel_used_pct,
    count(*)                  FILTER (WHERE NOT t.is_gps_outlier)          AS valid_points,
    ST_MakeLine(t.geom ORDER BY t.event_ts) FILTER (WHERE NOT t.is_gps_outlier) AS raw_track
  FROM silver.telemetry_enriched t
  GROUP BY t.vehicle_id, local_day
)
SELECT
  d.vehicle_id,
  d.local_day,
  d.points,
  d.first_event_ts,
  d.last_event_ts,
  round(coalesce(d.distance_km, 0)::numeric, 2)                             AS distance_km,
  round(d.avg_moving_speed_kmh::numeric, 1)                                 AS avg_moving_speed_kmh,
  round(d.max_speed_kmh::numeric, 1)                                        AS max_speed_kmh,
  d.speeding_points,
  round((100.0 * coalesce(d.speeding_s, 0) / nullif(d.tracked_s, 0))::numeric, 2) AS speeding_time_pct,
  round(coalesce(d.max_excess_kmh, 0)::numeric, 1)                          AS max_excess_kmh,
  d.harsh_events,
  round((d.harsh_events * 100.0 / nullif(d.distance_km, 0))::numeric, 2)    AS harsh_per_100km,
  d.refuels,
  round((coalesce(d.fuel_used_pct, 0) * v.fuel_tank_capacity_l / 100.0)::numeric, 2) AS fuel_used_l,
  round((coalesce(d.fuel_used_pct, 0) * v.fuel_tank_capacity_l
         / nullif(d.distance_km, 0))::numeric, 2)                           AS consumption_l_100km,
  d.low_fuel_risk_points,
  gold.driver_score(
    (100.0 * coalesce(d.speeding_s, 0) / nullif(d.tracked_s, 0))::numeric,
    (d.harsh_events * 100.0 / nullif(d.distance_km, 0))::numeric,
    d.max_excess_kmh::numeric
  )                                                                         AS driver_score,
  CASE WHEN d.valid_points >= 2 THEN d.raw_track END                        AS track_geom
FROM daily d
LEFT JOIN ref.vehicle v ON v.vehicle_id = d.vehicle_id
WITH DATA;

CREATE UNIQUE INDEX vehicle_daily_kpi_pk    ON gold.vehicle_daily_kpi (vehicle_id, local_day);
CREATE INDEX        vehicle_daily_kpi_day   ON gold.vehicle_daily_kpi (local_day);
CREATE INDEX        vehicle_daily_kpi_track ON gold.vehicle_daily_kpi USING gist (track_geom);

-- ---------------------------------------------------------------------
-- Infracciones desnormalizadas (capa de puntos del mapa de calor)
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW gold.infraction_point AS
SELECT
  a.alert_id,
  a.vehicle_id,
  a.event_ts,
  (a.event_ts AT TIME ZONE 'America/Bogota')::date AS local_day,
  a.alert_type,
  a.severity,
  a.geofence_id,
  g.name      AS geofence_name,
  g.zone_type,
  a.observed_value,
  a.threshold_value,
  a.geom
FROM silver.alert a
LEFT JOIN ref.geofence g ON g.geofence_id = a.geofence_id
WITH DATA;

CREATE UNIQUE INDEX infraction_point_pk   ON gold.infraction_point (alert_id);
CREATE INDEX        infraction_point_geom ON gold.infraction_point USING gist (geom);
CREATE INDEX        infraction_point_day  ON gold.infraction_point (local_day, alert_type);

-- ---------------------------------------------------------------------
-- Puntos calientes: malla de 250 m × 250 m en EPSG:9377 (metros reales)
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW gold.infraction_grid_250m AS
WITH snapped AS (
  SELECT
    a.alert_type,
    a.severity,
    a.event_ts,
    floor(ST_X(ST_Transform(a.geom, 9377)) / 250)::bigint AS cell_x,
    floor(ST_Y(ST_Transform(a.geom, 9377)) / 250)::bigint AS cell_y
  FROM silver.alert a
)
SELECT
  s.cell_x,
  s.cell_y,
  s.alert_type,
  count(*)                                                AS infractions,
  count(*) FILTER (WHERE s.severity IN ('HIGH', 'CRITICAL')) AS severe_infractions,
  min(s.event_ts)                                         AS first_seen,
  max(s.event_ts)                                         AS last_seen,
  ST_Transform(
    ST_MakeEnvelope(s.cell_x * 250, s.cell_y * 250,
                    (s.cell_x + 1) * 250, (s.cell_y + 1) * 250, 9377),
    4326)                                                 AS cell_geom
FROM snapped s
GROUP BY s.cell_x, s.cell_y, s.alert_type
WITH DATA;

CREATE UNIQUE INDEX infraction_grid_pk   ON gold.infraction_grid_250m (cell_x, cell_y, alert_type);
CREATE INDEX        infraction_grid_geom ON gold.infraction_grid_250m USING gist (cell_geom);

-- ---------------------------------------------------------------------
-- Zonas calientes: agrupamiento DBSCAN de los excesos de velocidad
-- ---------------------------------------------------------------------
-- La malla cuenta infracciones por celda, pero en un mapa son decenas de
-- cuadros sueltos. Aquí se agrupan los puntos que tienen al menos 3 excesos a
-- menos de 600 m (ST_ClusterDBSCAN, en metros de EPSG:9377); los excesos
-- aislados quedan como ruido y no forman zona. Cada zona es la envolvente
-- convexa de sus puntos con un margen de 150 m: pocos polígonos, cada uno con
-- un significado (dónde se repite el exceso).
CREATE MATERIALIZED VIEW gold.infraction_hotspot AS
WITH pts AS (
  SELECT a.vehicle_id, a.severity, a.event_ts, ST_Transform(a.geom, 9377) AS g
  FROM silver.alert a
  WHERE a.alert_type = 'SPEEDING'
), clustered AS (
  SELECT p.*, ST_ClusterDBSCAN(p.g, eps => 600, minpoints => 3) OVER () AS cluster_id
  FROM pts p
), zones AS (
  SELECT
    count(*)                                                AS infractions,
    count(*) FILTER (WHERE severity IN ('HIGH', 'CRITICAL')) AS severe_infractions,
    count(DISTINCT vehicle_id)                              AS vehicles,
    min(event_ts)                                           AS first_seen,
    max(event_ts)                                           AS last_seen,
    ST_Buffer(ST_ConvexHull(ST_Collect(g)), 150, 'quad_segs=4') AS g
  FROM clustered
  WHERE cluster_id IS NOT NULL
  GROUP BY cluster_id
)
SELECT
  row_number() OVER (ORDER BY infractions DESC, severe_infractions DESC, first_seen)::int
                                                          AS hotspot_rank,
  infractions,
  severe_infractions,
  vehicles,
  first_seen,
  last_seen,
  round((ST_Area(g) / 1e6)::numeric, 3)                   AS area_km2,
  ST_Transform(ST_SimplifyPreserveTopology(g, 10), 4326)  AS geom
FROM zones
WITH DATA;

CREATE UNIQUE INDEX infraction_hotspot_pk   ON gold.infraction_hotspot (hotspot_rank);
CREATE INDEX        infraction_hotspot_geom ON gold.infraction_hotspot USING gist (geom);

-- ---------------------------------------------------------------------
-- Refresco de la capa Oro (lo invoca el worker ETL tras cada lote)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gold.refresh_all(p_concurrently boolean DEFAULT true)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
-- public: algunas funciones SQL de PostGIS resuelven otras por nombre no calificado.
-- Es seguro porque PUBLIC no tiene CREATE en public (ver 01_schemas_ops.sql).
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF p_concurrently THEN
    REFRESH MATERIALIZED VIEW CONCURRENTLY gold.vehicle_daily_kpi;
    REFRESH MATERIALIZED VIEW CONCURRENTLY gold.infraction_point;
    REFRESH MATERIALIZED VIEW CONCURRENTLY gold.infraction_grid_250m;
    REFRESH MATERIALIZED VIEW CONCURRENTLY gold.infraction_hotspot;
  ELSE
    REFRESH MATERIALIZED VIEW gold.vehicle_daily_kpi;
    REFRESH MATERIALIZED VIEW gold.infraction_point;
    REFRESH MATERIALIZED VIEW gold.infraction_grid_250m;
    REFRESH MATERIALIZED VIEW gold.infraction_hotspot;
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION gold.refresh_all(boolean) FROM PUBLIC;
