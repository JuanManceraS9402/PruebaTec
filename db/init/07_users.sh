#!/usr/bin/env bash
# =====================================================================
# 07 · Usuarios de login (contraseñas desde variables de entorno)
# =====================================================================
# Las contraseñas se pasan como variables de psql (:'var'), que psql
# escapa como literales: no hay concatenación de strings en SQL.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v ingest_pw="${APP_INGEST_PASSWORD:-ingest_dev}" \
     -v etl_pw="${APP_ETL_PASSWORD:-etl_dev}" \
     -v analyst_pw="${APP_ANALYST_PASSWORD:-analyst_dev}" \
     -v admin_pw="${APP_ADMIN_PASSWORD:-admin_dev}" <<'EOSQL'
CREATE ROLE api_ingest   LOGIN PASSWORD :'ingest_pw'  IN ROLE telemetry_ingest;
CREATE ROLE etl_worker   LOGIN PASSWORD :'etl_pw'     IN ROLE pipeline_worker;
CREATE ROLE analyst_demo LOGIN PASSWORD :'analyst_pw' IN ROLE bi_analyst;
CREATE ROLE admin_demo   LOGIN PASSWORD :'admin_pw'   IN ROLE fleet_admin;

-- Límites defensivos para los usuarios interactivos
ALTER ROLE analyst_demo SET statement_timeout = '30s';
ALTER ROLE admin_demo   SET statement_timeout = '60s';
EOSQL

echo "Usuarios de aplicación creados: api_ingest, etl_worker, analyst_demo, admin_demo"
