-- =====================================================================
-- 08 · Simulación bajo demanda ("¿qué pasa si este vehículo va por esta ruta?")
-- =====================================================================
-- El tablero no usa la cuenta dueña del esquema. Un administrador solo puede
-- crear y borrar vehículos marcados como SANDBOX, a través de estas funciones;
-- la flota real no se puede tocar por este camino.

CREATE OR REPLACE VIEW analytics.sandbox_vehicle AS
SELECT vp.vehicle_key
FROM ref.vehicle v
JOIN security.vehicle_pseudonym vp USING (vehicle_id)
WHERE v.vehicle_type = 'SANDBOX';

GRANT SELECT ON analytics.sandbox_vehicle TO bi_analyst, fleet_admin;

CREATE OR REPLACE FUNCTION ops.sandbox_register(
  p_vehicle_id  text,
  p_tank_l      numeric,
  p_consumption numeric,
  p_route_code  text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  v_route_id integer;
BEGIN
  -- Serializa los registros para que el tope no se pueda superar en paralelo
  PERFORM pg_catalog.pg_advisory_xact_lock(hashtext('ops.sandbox_register'));

  IF (SELECT count(*) FROM ref.vehicle WHERE vehicle_type = 'SANDBOX') >= 20 THEN
    RAISE EXCEPTION 'Máximo 20 vehículos simulados: borra los anteriores';
  END IF;

  SELECT route_id INTO v_route_id FROM ref.route WHERE code = p_route_code;
  IF v_route_id IS NULL THEN
    RAISE EXCEPTION 'La ruta % no existe', p_route_code;
  END IF;

  INSERT INTO ref.vehicle
    (vehicle_id, vehicle_type, fuel_tank_capacity_l, nominal_consumption_l_100km)
  VALUES (p_vehicle_id, 'SANDBOX', p_tank_l, p_consumption)
  ON CONFLICT (vehicle_id) DO NOTHING;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'El vehículo % ya existe', p_vehicle_id USING ERRCODE = 'unique_violation';
  END IF;

  INSERT INTO ref.vehicle_route_assignment (vehicle_id, route_id, valid_from)
  VALUES (p_vehicle_id, v_route_id, clock_timestamp());
END;
$$;

-- Igual que sandbox_register, pero con una ruta nueva (código SBX-<placa>). Es una
-- sola transacción: si la placa ya existe, la ruta tampoco queda.
CREATE OR REPLACE FUNCTION ops.sandbox_register_with_route(
  p_vehicle_id  text,
  p_tank_l      numeric,
  p_consumption numeric,
  p_route_code  text,
  p_route_name  text,
  p_route_wkb   bytea
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
-- public: PostGIS resuelve algunas de sus funciones por nombre no calificado
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF p_route_code IS DISTINCT FROM 'SBX-' || p_vehicle_id THEN
    RAISE EXCEPTION 'Código de ruta simulada inválido: %', p_route_code;
  END IF;

  INSERT INTO ref.route (code, name, geom)
  VALUES (p_route_code, left(p_route_name, 200), ST_GeomFromWKB(p_route_wkb, 4326));

  PERFORM ops.sandbox_register(p_vehicle_id, p_tank_l, p_consumption, p_route_code);
EXCEPTION
  WHEN unique_violation THEN
    RAISE EXCEPTION 'El vehículo % ya existe', p_vehicle_id USING ERRCODE = 'unique_violation';
END;
$$;

-- Borra un vehículo simulado, o todos si no se indica ninguno. Devuelve cuántos.
CREATE OR REPLACE FUNCTION ops.sandbox_purge(p_vehicle_id text DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  v_ids text[];
BEGIN
  SELECT coalesce(array_agg(vehicle_id), '{}') INTO v_ids
  FROM ref.vehicle
  WHERE vehicle_type = 'SANDBOX'
    AND (p_vehicle_id IS NULL OR vehicle_id = p_vehicle_id);

  DELETE FROM silver.alert WHERE vehicle_id = ANY (v_ids);
  DELETE FROM silver.telemetry_enriched WHERE vehicle_id = ANY (v_ids);
  DELETE FROM bronze.telemetry_raw WHERE vehicle_id = ANY (v_ids);
  DELETE FROM ref.vehicle WHERE vehicle_id = ANY (v_ids);  -- asignación y seudónimo en cascada
  DELETE FROM ref.route
  WHERE code IN (SELECT 'SBX-' || id FROM unnest(v_ids) AS id);
  RETURN cardinality(v_ids);
END;
$$;

REVOKE ALL ON FUNCTION ops.sandbox_register(text, numeric, numeric, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops.sandbox_purge(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops.sandbox_register_with_route(text, numeric, numeric, text, text, bytea)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.sandbox_register_with_route(text, numeric, numeric, text, text, bytea)
  TO fleet_admin;
GRANT EXECUTE ON FUNCTION ops.sandbox_register(text, numeric, numeric, text) TO fleet_admin;
GRANT EXECUTE ON FUNCTION ops.sandbox_purge(text) TO fleet_admin;
