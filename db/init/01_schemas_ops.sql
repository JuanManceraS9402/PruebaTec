-- =====================================================================
-- 01 · Esquemas por capa + utilidades operativas
-- =====================================================================
--  ref       → datos maestros (vehículos, geocercas, rutas)
--  bronze    → telemetría cruda, inmutable, tal como llega del dispositivo
--  silver    → telemetría limpia y enriquecida + alertas
--  gold      → modelos agregados listos para BI
--  analytics → ÚNICA superficie expuesta a analistas (vistas enmascaradas)
--  security  → enmascaramiento / seudonimización
--  ops       → parámetros, watermarks, mantenimiento de particiones
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS security;
CREATE SCHEMA IF NOT EXISTS ops;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ---------------------------------------------------------------------
-- Parámetros de negocio centralizados: sin "números mágicos" en el código.
-- ---------------------------------------------------------------------
CREATE TABLE ops.parameter (
  name        text    PRIMARY KEY,
  value       numeric NOT NULL,
  unit        text,
  description text    NOT NULL
);

INSERT INTO ops.parameter (name, value, unit, description) VALUES
  ('default_speed_limit_kmh',   80,   'km/h',  'Límite aplicado cuando el punto no cae en ninguna geocerca con límite'),
  ('speed_tolerance_kmh',        3,   'km/h',  'Tolerancia por error de medición del GPS antes de declarar exceso'),
  ('gps_max_implied_speed_kmh', 200,  'km/h',  'Velocidad implícita entre dos puntos por encima de la cual el punto es outlier'),
  ('harsh_accel_mps2',          3.0,  'm/s2',  'Aceleración brusca (positiva)'),
  ('harsh_brake_mps2',          3.5,  'm/s2',  'Frenada brusca (magnitud de la desaceleración)'),
  ('accel_max_gap_s',           30,   's',     'Hueco máximo entre reportes para calcular aceleración (más allá no es fiable)'),
  ('refuel_min_jump_pct',       5,    '%',     'Subida mínima de combustible para considerar un tanqueo'),
  ('fuel_safety_factor',        1.15, 'ratio', 'Margen de seguridad: autonomía requerida = distancia restante × factor'),
  ('consumption_window_km',     30,   'km',    'Ventana móvil de distancia para estimar el consumo real L/100km'),
  ('min_consumption_sample_km', 3,    'km',    'Distancia mínima en la ventana para confiar en el consumo medido');

-- ---------------------------------------------------------------------
-- Watermarks del procesamiento incremental Bronce → Plata.
-- Se usa ingested_at (no event_ts) para no perder datos que llegan tarde.
-- ---------------------------------------------------------------------
CREATE TABLE ops.watermark (
  pipeline         text        PRIMARY KEY,
  last_ingested_at timestamptz NOT NULL DEFAULT '-infinity',
  updated_at       timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ops.watermark (pipeline) VALUES ('bronze_to_silver');

-- ---------------------------------------------------------------------
-- Creación idempotente de particiones diarias (UTC).
-- SECURITY DEFINER: los roles de ingesta/ETL pueden crear particiones
-- sin ser dueños de la tabla padre.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.ensure_daily_partitions(
  p_parent regclass,
  p_from   date,
  p_to     date
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  v_schema  text;
  v_table   text;
  v_day     date := p_from;
  v_part    text;
  v_created integer := 0;
BEGIN
  IF p_to < p_from THEN
    RAISE EXCEPTION 'Rango inválido: % es posterior a %', p_from, p_to;
  END IF;
  IF p_to - p_from > 366 THEN
    RAISE EXCEPTION 'Rango demasiado amplio (% días, máximo 366)', p_to - p_from;
  END IF;

  SELECT n.nspname, c.relname
    INTO v_schema, v_table
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.oid = p_parent
     AND c.relkind = 'p';

  IF v_table IS NULL THEN
    RAISE EXCEPTION '% no es una tabla particionada', p_parent;
  END IF;

  -- Evita la carrera entre workers que crean la misma partición a la vez.
  PERFORM pg_advisory_xact_lock(hashtext(p_parent::text));

  WHILE v_day <= p_to LOOP
    v_part := v_table || '_p' || to_char(v_day, 'YYYYMMDD');

    IF to_regclass(format('%I.%I', v_schema, v_part)) IS NULL THEN
      EXECUTE format(
        'CREATE TABLE %I.%I PARTITION OF %I.%I FOR VALUES FROM (%L) TO (%L)',
        v_schema, v_part, v_schema, v_table,
        v_day::timestamp       AT TIME ZONE 'UTC',
        (v_day + 1)::timestamp AT TIME ZONE 'UTC'
      );
      v_created := v_created + 1;
    END IF;

    v_day := v_day + 1;
  END LOOP;

  RETURN v_created;
END;
$$;

COMMENT ON FUNCTION ops.ensure_daily_partitions IS
  'Crea (si no existen) las particiones diarias UTC de p_parent entre p_from y p_to, ambos inclusive.';

REVOKE ALL ON FUNCTION ops.ensure_daily_partitions(regclass, date, date) FROM PUBLIC;
