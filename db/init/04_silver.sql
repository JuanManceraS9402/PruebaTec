-- =====================================================================
-- 04 · Capa Plata: telemetría limpia, enriquecida y alertas
-- =====================================================================

CREATE TABLE silver.telemetry_enriched (
  vehicle_id              text        NOT NULL,
  event_ts                timestamptz NOT NULL,
  geom                    geometry(Point, 4326) NOT NULL,
  speed_kmh               real        NOT NULL,
  fuel_level_pct          real        NOT NULL,

  -- Cinemática respecto al punto anterior del mismo vehículo
  prev_event_ts           timestamptz,
  delta_s                 double precision,
  segment_distance_m      double precision,   -- geodésica (geography)
  implied_speed_kmh       double precision,   -- distancia / tiempo entre puntos
  accel_mps2              double precision,
  fuel_delta_pct          real,
  is_refuel               boolean     NOT NULL DEFAULT false,
  is_gps_outlier          boolean     NOT NULL DEFAULT false,
  is_harsh_event          boolean     NOT NULL DEFAULT false,

  -- Geocercas y velocidad
  geofence_ids            integer[]   NOT NULL DEFAULT '{}',
  applied_speed_limit_kmh smallint    NOT NULL,
  speed_excess_kmh        real        NOT NULL DEFAULT 0,
  is_speeding             boolean     NOT NULL DEFAULT false,

  -- Combustible predictivo
  consumption_l_100km     real,       -- consumo real estimado en ventana móvil
  est_range_km            real,       -- autonomía con el combustible actual
  remaining_route_km      real,       -- distancia que falta para terminar la ruta
  is_low_fuel_risk        boolean     NOT NULL DEFAULT false,

  processed_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (vehicle_id, event_ts)
) PARTITION BY RANGE (event_ts);

CREATE INDEX telemetry_enriched_geom_gist  ON silver.telemetry_enriched USING gist (geom);
CREATE INDEX telemetry_enriched_event_brin ON silver.telemetry_enriched USING brin (event_ts) WITH (pages_per_range = 32);
-- "¿Qué puntos estuvieron en la geocerca X?" sin tocar geometrías
CREATE INDEX telemetry_enriched_geofences_gin ON silver.telemetry_enriched USING gin (geofence_ids);
-- Índice parcial: los excesos son una fracción pequeña del total
CREATE INDEX telemetry_enriched_speeding_idx
  ON silver.telemetry_enriched (event_ts) WHERE is_speeding;

CREATE TABLE silver.telemetry_enriched_default PARTITION OF silver.telemetry_enriched DEFAULT;

SELECT ops.ensure_daily_partitions('silver.telemetry_enriched', current_date - 7, current_date + 30);

-- ---------------------------------------------------------------------
-- Alertas (volumen bajo frente a la telemetría → tabla no particionada)
-- ---------------------------------------------------------------------
CREATE TYPE silver.alert_type     AS ENUM ('SPEEDING', 'LOW_FUEL_RANGE');
CREATE TYPE silver.alert_severity AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL');

CREATE TABLE silver.alert (
  alert_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vehicle_id      text                  NOT NULL,
  event_ts        timestamptz           NOT NULL,
  alert_type      silver.alert_type     NOT NULL,
  severity        silver.alert_severity NOT NULL,
  geofence_id     integer REFERENCES ref.geofence,
  geom            geometry(Point, 4326) NOT NULL,
  observed_value  real,                 -- p.ej. velocidad medida / autonomía km
  threshold_value real,                 -- p.ej. límite aplicado / km requeridos
  details         jsonb                 NOT NULL DEFAULT '{}',
  created_at      timestamptz           NOT NULL DEFAULT now(),
  -- Reprocesar un lote no duplica alertas
  UNIQUE (vehicle_id, event_ts, alert_type)
);

CREATE INDEX alert_geom_gist ON silver.alert USING gist (geom);
CREATE INDEX alert_event_ts  ON silver.alert (event_ts);
CREATE INDEX alert_type_ts   ON silver.alert (alert_type, event_ts);
