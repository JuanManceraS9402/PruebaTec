-- =====================================================================
-- 06 · Seguridad: roles, enmascaramiento y superficie analítica
-- =====================================================================
-- Privacidad por diseño, en la base de datos (no solo en la API):
--  * Los analistas NO tienen acceso a bronze/silver/gold/ref; solo leen
--    el esquema analytics, cuyas vistas enmascaran el ID del vehículo.
--  * El enmascaramiento depende del rol de quien consulta: los miembros
--    de fleet_admin ven el ID real con la misma vista.
--  * Para que el analista pueda agrupar por vehículo sin conocer la placa,
--    se expone un seudónimo estable (vehicle_key = SHA-256 con sal secreta).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Roles de grupo (sin login). Los usuarios de login están en 07_users.sh
-- ---------------------------------------------------------------------
CREATE ROLE telemetry_ingest NOLOGIN;  -- API de ingesta: solo INSERT en Bronce
CREATE ROLE pipeline_worker  NOLOGIN;  -- ETL: lee Bronce, escribe Plata, refresca Oro
CREATE ROLE bi_analyst       NOLOGIN;  -- BI: solo analytics.*, IDs enmascarados
CREATE ROLE fleet_admin      NOLOGIN;  -- administrador: datos sin enmascarar

GRANT bi_analyst TO fleet_admin;

-- ---------------------------------------------------------------------
-- Funciones de enmascaramiento
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION security.mask_vehicle_id(p_vehicle_id text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
  -- VHC-8472-ABC → VHC-****-ABC ; cualquier formato inesperado se oculta entero
  SELECT CASE
           WHEN p_vehicle_id ~ '^[A-Z]{3}-[0-9]{4}-[A-Z]{3}$'
             THEN substr(p_vehicle_id, 1, 4) || '****' || substr(p_vehicle_id, 9)
           ELSE '***MASKED***'
         END
$$;

CREATE OR REPLACE FUNCTION security.is_admin()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
  SELECT pg_has_role(current_user, 'fleet_admin', 'MEMBER')
$$;

-- ---------------------------------------------------------------------
-- Seudonimización estable: tabla precalculada (un hash por vehículo,
-- no por fila consultada) mantenida por trigger.
-- ---------------------------------------------------------------------
CREATE TABLE security.secret (
  name  text PRIMARY KEY,
  value text NOT NULL
);

INSERT INTO security.secret (name, value)
VALUES ('pseudonym_salt', gen_random_uuid()::text);

CREATE TABLE security.vehicle_pseudonym (
  vehicle_id  text PRIMARY KEY REFERENCES ref.vehicle ON DELETE CASCADE,
  vehicle_key text NOT NULL UNIQUE,
  -- Precalculado: enmascarar con la expresión regular en cada fila dominaba el
  -- tiempo de las consultas de BI (155 de 158 ms en 18.000 infracciones).
  masked_id   text NOT NULL
);

CREATE OR REPLACE FUNCTION security.compute_vehicle_key(p_vehicle_id text)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT 'VK-' || left(encode(sha256(convert_to(s.value || ':' || p_vehicle_id, 'UTF8')), 'hex'), 12)
    FROM security.secret s
   WHERE s.name = 'pseudonym_salt'
$$;

-- Si un analista pudiera ejecutarla, podría probar placas por fuerza bruta.
REVOKE ALL ON FUNCTION security.compute_vehicle_key(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION security.trg_vehicle_pseudonym()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
  INSERT INTO security.vehicle_pseudonym (vehicle_id, vehicle_key, masked_id)
  VALUES (NEW.vehicle_id, security.compute_vehicle_key(NEW.vehicle_id),
          security.mask_vehicle_id(NEW.vehicle_id))
  ON CONFLICT (vehicle_id) DO NOTHING;
  RETURN NEW;
END;
$$;

CREATE TRIGGER vehicle_pseudonym_ai
  AFTER INSERT ON ref.vehicle
  FOR EACH ROW EXECUTE FUNCTION security.trg_vehicle_pseudonym();

-- ---------------------------------------------------------------------
-- Superficie analítica (security_barrier evita que un predicado del
-- usuario se evalúe antes del enmascaramiento y filtre información).
-- is_admin() se evalúa una vez por consulta (subconsulta ctx), no por fila.
-- ---------------------------------------------------------------------
CREATE VIEW analytics.telemetry_raw WITH (security_barrier = true) AS
SELECT
  CASE WHEN ctx.is_admin THEN t.vehicle_id
       ELSE coalesce(vp.masked_id, security.mask_vehicle_id(t.vehicle_id)) END AS vehicle_id,
  vp.vehicle_key,
  t.event_ts,
  t.lat,
  t.lon,
  t.speed_kmh,
  t.fuel_level_pct,
  t.geom,
  t.ingested_at
FROM bronze.telemetry_raw t
CROSS JOIN (SELECT security.is_admin() AS is_admin) ctx
LEFT JOIN security.vehicle_pseudonym vp ON vp.vehicle_id = t.vehicle_id;

CREATE VIEW analytics.telemetry_enriched WITH (security_barrier = true) AS
SELECT
  CASE WHEN ctx.is_admin THEN t.vehicle_id
       ELSE coalesce(vp.masked_id, security.mask_vehicle_id(t.vehicle_id)) END AS vehicle_id,
  vp.vehicle_key,
  t.event_ts,
  t.geom,
  t.speed_kmh,
  t.fuel_level_pct,
  t.segment_distance_m,
  t.accel_mps2,
  t.is_gps_outlier,
  t.is_harsh_event,
  t.geofence_ids,
  t.applied_speed_limit_kmh,
  t.speed_excess_kmh,
  t.is_speeding,
  t.consumption_l_100km,
  t.est_range_km,
  t.remaining_route_km,
  t.is_low_fuel_risk
FROM silver.telemetry_enriched t
CROSS JOIN (SELECT security.is_admin() AS is_admin) ctx
LEFT JOIN security.vehicle_pseudonym vp ON vp.vehicle_id = t.vehicle_id;

CREATE VIEW analytics.alert WITH (security_barrier = true) AS
SELECT
  a.alert_id,
  CASE WHEN ctx.is_admin THEN a.vehicle_id
       ELSE coalesce(vp.masked_id, security.mask_vehicle_id(a.vehicle_id)) END AS vehicle_id,
  vp.vehicle_key,
  a.event_ts,
  a.alert_type,
  a.severity,
  a.geofence_id,
  a.geom,
  a.observed_value,
  a.threshold_value,
  a.details
FROM silver.alert a
CROSS JOIN (SELECT security.is_admin() AS is_admin) ctx
LEFT JOIN security.vehicle_pseudonym vp ON vp.vehicle_id = a.vehicle_id;

CREATE VIEW analytics.vehicle_daily_kpi WITH (security_barrier = true) AS
SELECT
  CASE WHEN ctx.is_admin THEN k.vehicle_id
       ELSE coalesce(vp.masked_id, security.mask_vehicle_id(k.vehicle_id)) END AS vehicle_id,
  vp.vehicle_key,
  k.local_day,
  k.points,
  k.first_event_ts,
  k.last_event_ts,
  k.distance_km,
  k.avg_moving_speed_kmh,
  k.max_speed_kmh,
  k.speeding_points,
  k.speeding_time_pct,
  k.max_excess_kmh,
  k.harsh_events,
  k.harsh_per_100km,
  k.refuels,
  k.fuel_used_l,
  k.consumption_l_100km,
  k.low_fuel_risk_points,
  k.driver_score,
  k.track_geom,
  k.track_map
FROM gold.vehicle_daily_kpi k
CROSS JOIN (SELECT security.is_admin() AS is_admin) ctx
LEFT JOIN security.vehicle_pseudonym vp ON vp.vehicle_id = k.vehicle_id;

CREATE VIEW analytics.infraction_point WITH (security_barrier = true) AS
SELECT
  p.alert_id,
  CASE WHEN ctx.is_admin THEN p.vehicle_id
       ELSE coalesce(vp.masked_id, security.mask_vehicle_id(p.vehicle_id)) END AS vehicle_id,
  vp.vehicle_key,
  p.event_ts,
  p.local_day,
  p.alert_type,
  p.severity,
  p.geofence_id,
  p.geofence_name,
  p.zone_type,
  p.observed_value,
  p.threshold_value,
  p.geom
FROM gold.infraction_point p
CROSS JOIN (SELECT security.is_admin() AS is_admin) ctx
LEFT JOIN security.vehicle_pseudonym vp ON vp.vehicle_id = p.vehicle_id;

-- Sin datos personales: se exponen tal cual
CREATE VIEW analytics.infraction_grid_250m AS
SELECT * FROM gold.infraction_grid_250m;

CREATE VIEW analytics.infraction_hotspot AS
SELECT * FROM gold.infraction_hotspot;

-- Capa del mapa, SIN identificadores de vehículo (solo el seudónimo). Al no tener nada
-- que enmascarar no necesita security_barrier, y por eso un filtro espacial (&&) llega
-- al índice GiST: PostgreSQL no empuja operadores no leakproof a través de una barrera.
CREATE VIEW analytics.infraction_map AS
SELECT
  p.alert_id,
  vp.vehicle_key,
  p.event_ts,
  p.local_day,
  p.alert_type,
  p.severity,
  p.geofence_name,
  p.zone_type,
  p.geom
FROM gold.infraction_point p
LEFT JOIN security.vehicle_pseudonym vp ON vp.vehicle_id = p.vehicle_id;

CREATE VIEW analytics.geofence AS
SELECT geofence_id, code, name, zone_type, speed_limit_kmh, geom
FROM ref.geofence
WHERE active;

-- ---------------------------------------------------------------------
-- Privilegios (mínimo privilegio por rol)
-- ---------------------------------------------------------------------
-- Ingesta
GRANT USAGE  ON SCHEMA bronze, ops TO telemetry_ingest;
GRANT INSERT ON bronze.telemetry_raw TO telemetry_ingest;
-- ON CONFLICT (vehicle_id, event_ts) necesita leer las columnas de la clave
GRANT SELECT (vehicle_id, event_ts) ON bronze.telemetry_raw TO telemetry_ingest;
GRANT EXECUTE ON FUNCTION ops.ensure_daily_partitions(regclass, date, date) TO telemetry_ingest;

-- ETL
GRANT USAGE ON SCHEMA ref, bronze, silver, gold, ops TO pipeline_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA ref TO pipeline_worker;
GRANT SELECT ON bronze.telemetry_raw TO pipeline_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA silver TO pipeline_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA silver TO pipeline_worker;
GRANT SELECT ON ops.parameter TO pipeline_worker;
GRANT SELECT, UPDATE ON ops.watermark TO pipeline_worker;
GRANT EXECUTE ON FUNCTION ops.ensure_daily_partitions(regclass, date, date) TO pipeline_worker;
GRANT EXECUTE ON FUNCTION gold.refresh_all(boolean) TO pipeline_worker;

-- Analistas: únicamente la superficie analytics
GRANT USAGE  ON SCHEMA analytics, security TO bi_analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO bi_analyst;

-- Administradores: además, lectura directa de todas las capas
GRANT USAGE  ON SCHEMA ref, bronze, silver, gold, ops TO fleet_admin;
GRANT SELECT ON ALL TABLES IN SCHEMA ref, bronze, silver, gold TO fleet_admin;
GRANT SELECT ON ops.parameter, ops.watermark TO fleet_admin;
