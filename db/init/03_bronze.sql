-- =====================================================================
-- 03 · Capa Bronce: telemetría cruda IoT/AVL
-- =====================================================================
-- Decisiones (detalle en DESIGN.md):
--  * Particionado por RANGE diario sobre event_ts: el pruning limita cada
--    consulta a los días pedidos y la retención es un DROP de partición.
--  * PK natural (vehicle_id, event_ts): los equipos AVL retransmiten
--    paquetes; INSERT ... ON CONFLICT DO NOTHING hace la ingesta idempotente.
--  * geom es una columna GENERADA a partir de lon/lat: elimina el error
--    clásico de invertir el orden (ST_MakePoint recibe X=lon, Y=lat).
--  * Sin FK hacia ref.vehicle: Bronce no debe rechazar datos por un maestro
--    desactualizado, y la FK cuesta en ingesta masiva. Plata hace la validación.
--  * CHECKs solo de validez física; los outliers semánticos (saltos de GPS)
--    se conservan aquí y se marcan en Plata.
-- =====================================================================

CREATE TABLE bronze.telemetry_raw (
  vehicle_id     text             NOT NULL,
  event_ts       timestamptz      NOT NULL,
  lat            double precision NOT NULL CHECK (lat BETWEEN -90  AND 90),
  lon            double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
  speed_kmh      real             NOT NULL CHECK (speed_kmh BETWEEN 0 AND 300),
  fuel_level_pct real             NOT NULL CHECK (fuel_level_pct BETWEEN 0 AND 100),
  geom           geometry(Point, 4326)
                 GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
  source         text             NOT NULL DEFAULT 'api',
  ingested_at    timestamptz      NOT NULL DEFAULT now(),
  PRIMARY KEY (vehicle_id, event_ts)
) PARTITION BY RANGE (event_ts);

COMMENT ON TABLE bronze.telemetry_raw IS
  'Paquetes AVL tal como llegan. Inmutable: solo INSERT. Particionada por día (UTC).';

-- Los índices declarados en la tabla padre se propagan a cada partición.
-- GiST: consultas espaciales (bbox, geocercas, cercanía).
CREATE INDEX telemetry_raw_geom_gist ON bronze.telemetry_raw USING gist (geom);
-- BRIN: los datos llegan casi ordenados en el tiempo → índice de pocos KB
-- frente a cientos de MB de un B-tree equivalente.
CREATE INDEX telemetry_raw_event_brin    ON bronze.telemetry_raw USING brin (event_ts)    WITH (pages_per_range = 32);
CREATE INDEX telemetry_raw_ingested_brin ON bronze.telemetry_raw USING brin (ingested_at) WITH (pages_per_range = 32);

-- Partición DEFAULT: red de seguridad para timestamps fuera de rango
-- (relojes de equipo desfasados). Debe vigilarse: si acumula filas de un
-- día, crear después la partición de ese día falla hasta moverlas.
CREATE TABLE bronze.telemetry_raw_default PARTITION OF bronze.telemetry_raw DEFAULT;

-- Particiones iniciales: 7 días hacia atrás y 30 hacia adelante.
SELECT ops.ensure_daily_partitions('bronze.telemetry_raw', current_date - 7, current_date + 30);
