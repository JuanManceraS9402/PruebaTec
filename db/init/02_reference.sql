-- =====================================================================
-- 02 · Datos maestros: vehículos, geocercas y rutas planificadas
-- =====================================================================

CREATE TABLE ref.vehicle (
  vehicle_id                  text         PRIMARY KEY
                              CHECK (vehicle_id ~ '^[A-Z]{3}-[0-9]{4}-[A-Z]{3}$'),
  vehicle_type                text         NOT NULL DEFAULT 'TRUCK',
  fuel_tank_capacity_l        numeric(6,1) NOT NULL CHECK (fuel_tank_capacity_l > 0),
  nominal_consumption_l_100km numeric(5,2) NOT NULL CHECK (nominal_consumption_l_100km > 0),
  active                      boolean      NOT NULL DEFAULT true,
  created_at                  timestamptz  NOT NULL DEFAULT now()
);

COMMENT ON COLUMN ref.vehicle.nominal_consumption_l_100km IS
  'Consumo de ficha técnica; se usa como respaldo mientras no hay historial suficiente para estimar el consumo real.';

-- ---------------------------------------------------------------------
-- Geocercas
-- ---------------------------------------------------------------------
CREATE TYPE ref.zone_type AS ENUM (
  'URBAN',       -- perímetro urbano
  'SCHOOL',      -- zona escolar
  'HIGH_RISK',   -- alta siniestralidad
  'HIGHWAY',     -- corredor vial
  'DEPOT',       -- patio / centro logístico
  'RESTRICTED'   -- zona restringida
);

CREATE TABLE ref.geofence (
  geofence_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code            text    NOT NULL UNIQUE,
  name            text    NOT NULL,
  zone_type       ref.zone_type NOT NULL,
  -- NULL = la geocerca no impone límite (p.ej. una zona solo de monitoreo)
  speed_limit_kmh smallint CHECK (speed_limit_kmh BETWEEN 5 AND 130),
  geom            geometry(MultiPolygon, 4326) NOT NULL
                  CHECK (ST_IsValid(geom) AND NOT ST_IsEmpty(geom)),
  active          boolean     NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Índice espacial: el join punto-en-polígono (ST_Intersects) lo usa para
-- descartar por bounding box antes del cálculo exacto.
CREATE INDEX geofence_geom_gist ON ref.geofence USING gist (geom) WHERE active;

-- ---------------------------------------------------------------------
-- Rutas planificadas (base de la "autonomía de ruta" para combustible)
-- ---------------------------------------------------------------------
CREATE TABLE ref.route (
  route_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code       text    NOT NULL UNIQUE,
  name       text    NOT NULL,
  geom       geometry(LineString, 4326) NOT NULL
             CHECK (ST_IsValid(geom) AND ST_NPoints(geom) >= 2),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX route_geom_gist ON ref.route USING gist (geom);

CREATE TABLE ref.vehicle_route_assignment (
  vehicle_id text        NOT NULL REFERENCES ref.vehicle ON DELETE CASCADE,
  route_id   integer     NOT NULL REFERENCES ref.route   ON DELETE CASCADE,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to   timestamptz,
  PRIMARY KEY (vehicle_id, valid_from),
  CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- Un vehículo tiene como máximo una ruta activa.
CREATE UNIQUE INDEX vehicle_route_one_active
  ON ref.vehicle_route_assignment (vehicle_id)
  WHERE valid_to IS NULL;
