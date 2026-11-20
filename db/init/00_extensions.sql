-- =====================================================================
-- 00 · Extensiones y configuración base de la base de datos
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;  -- benchmark de consultas (DESIGN.md)

-- Toda la plataforma trabaja en UTC; la conversión a hora local
-- (America/Bogota) se hace solo en la capa Oro.
DO $$
BEGIN
  EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'UTC');
END
$$;
SET timezone TO 'UTC';

-- EPSG:9377 · MAGNA-SIRGAS / Origen-Nacional (CTM12), sistema métrico
-- oficial de Colombia. Se usa para agregaciones en malla (metros reales).
-- Se registra solo si la versión de PostGIS no lo trae.
INSERT INTO spatial_ref_sys (srid, auth_name, auth_srid, srtext, proj4text)
SELECT 9377, 'EPSG', 9377, NULL,
       '+proj=tmerc +lat_0=4 +lon_0=-73 +k=0.9992 +x_0=5000000 +y_0=2000000 '
       '+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs'
WHERE NOT EXISTS (SELECT 1 FROM spatial_ref_sys WHERE srid = 9377);
