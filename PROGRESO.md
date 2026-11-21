# PROGRESO · Estado del trabajo y cómo continuar

> Notas de traspaso para seguir en otro PC. Se pueden borrar antes de la entrega final,
> o dejarlas como bitácora.

## 1. Contexto

- **Prueba técnica:** Ingeniero GIS / Desarrollador de Datos Espaciales (Simón Movilidad, BI).
- **Objetivo:** un pipeline geoespacial **Bronce → Plata → Oro** para telemetría AVL de flota. Incluye:
  - ingesta con enmascaramiento `VHC-****-ABC`;
  - geocercas;
  - alertas de velocidad según la zona;
  - alerta predictiva de combustible;
  - capa Oro con índices GiST;
  - dashboard opcional.
- **Entregables:** repo público, tests, `DESIGN.md`, `SETUP.md` y un video de 5 minutos como máximo.
- **Pesos de la evaluación:** código 30 %, funcionalidad espacial 25 %, Big Data/SQL 20 %, documentación 15 %, testing 10 %.
- **Escenario:** Bogotá.
  - **Límite general de 50 km/h**, que incluye autopistas y vías principales.
  - **30 km/h** en zonas escolares.
  - **20 km/h** en el patio, como política interna de la empresa (está documentado así).

## 2. Restricciones del PC anterior

> **PC nuevo (2026-11-20):** tampoco tiene Docker (el usuario lo está instalando).
> **La CI ya pasó en verde, incluida la integración**: el SQL de `db/init` funciona en PostGIS real. El repo ya está en GitHub
> (`JuanManceraS9402/PruebaTec`) y la CI es la que ejecuta el SQL contra PostGIS.

- No se podía instalar nada: **no había Docker ni PostgreSQL**. Por eso **el SQL nunca se ha ejecutado**.
  - Solo se validó la sintaxis con `pglast`.
  - La ejecución real la hará la CI de GitHub Actions.
- **No se hizo ningún commit.** La identidad git de ese PC era de otra persona.

## 3. Primeros pasos en el PC nuevo

```bash
# 1. Copiar la carpeta SIN .venv/ y entrar en ella
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev,dashboard]"

# 2. Verificar que todo sigue verde
ruff check . && ruff format --check .
pytest -m "not integration"       # esperado: 133 passed

# 3. Git con tu identidad (local al repo)
git init -b main
git config user.name "Tu Nombre"
git config user.email "tu-correo-de-github"
git add -A
git commit -m "Pipeline geoespacial Bronce/Plata/Oro: esquema PostGIS, reglas, API y CI"

# 4. Crear el repo público en GitHub y subir
git remote add origin https://github.com/<usuario>/<repo>.git
git push -u origin main
```

Si el PC nuevo tiene Docker, conviene ejecutar el flujo de `SETUP.md`:

```bash
docker compose up -d --wait
python -m simon_geo.seed
REQUIRE_DB=1 pytest -m integration -v
```

**Es la primera vez que el SQL va a correr de verdad. Es probable que algo falle.** Los puntos de riesgo:

- `REFRESH MATERIALIZED VIEW CONCURRENTLY` dentro de `gold.refresh_all` (plpgsql).
- Los `GRANT` sobre vistas de `analytics` y las funciones SECURITY DEFINER.
- `07_users.sh` necesita finales de línea LF. `.gitattributes` ya lo fuerza.
- La inserción de EPSG:9377 en `spatial_ref_sys`, si la imagen ya la trae.

## 4. Lo que está hecho

### Base de datos (`db/init/`, se ejecuta en orden al crear el volumen)

| Archivo | Contenido |
|---|---|
| `00_extensions.sql` | PostGIS, `pg_stat_statements`, zona horaria UTC, EPSG:9377 (MAGNA-SIRGAS Origen-Nacional) |
| `01_schemas_ops.sql` | Esquemas `ref/bronze/silver/gold/analytics/security/ops`, tabla `ops.parameter` (parámetros de negocio), `ops.watermark`, función `ops.ensure_daily_partitions()` |
| `02_reference.sql` | `ref.vehicle`, `ref.geofence` (MultiPolygon, prioridad por tipo, GiST WHERE active), `ref.route`, `ref.vehicle_route_assignment` |
| `03_bronze.sql` | `bronze.telemetry_raw`: particionada por día, PK `(vehicle_id, event_ts)` para idempotencia, `geom` generada, índices GiST y BRIN, partición DEFAULT |
| `04_silver.sql` | `silver.telemetry_enriched` (columnas iguales a `EnrichedPoint`) y `silver.alert` con UNIQUE `(vehicle_id, event_ts, alert_type)` |
| `05_gold.sql` | Función `gold.driver_score()`. MVs `vehicle_daily_kpi`, `infraction_point` e `infraction_grid_250m` (celdas en EPSG:9377). `gold.refresh_all()` |
| `06_security.sql` | Roles `telemetry_ingest`, `pipeline_worker`, `bi_analyst`, `fleet_admin`. `mask_vehicle_id`, seudónimo `VK-…` con sal (SHA-256). Vistas `analytics.*` con `security_barrier` |
| `07_users.sh` | Usuarios de login `api_ingest`, `etl_worker`, `analyst_demo`, `admin_demo` |

### Python (`src/simon_geo/`)

| Módulo | Contenido |
|---|---|
| `config.py`, `db.py` | Settings (pydantic-settings, `.env`), `DbRole`, `connect()`, `PoolRegistry` (un pool por rol) |
| `parameters.py` | `PipelineParameters`: espejo de `ops.parameter`. Un test verifica que coinciden |
| `domain.py` | Vehicle, Geofence, TelemetryPoint, Alert, ZoneType, Severity |
| `spatial/` | `crs.py` (EPSG:9377), `distance.py` (haversine), `geofencing.py` (STRtree con prioridad SCHOOL > RESTRICTED > HIGH_RISK > DEPOT > HIGHWAY > URBAN; a igual prioridad gana el límite menor), `routes.py` (progreso y km restantes de la ruta) |
| `rules/` | `speed.py` (tolerancia de 3 km/h; severidad que escala en zonas SCHOOL y HIGH_RISK), `kinematics.py`, `fuel.py` (regresión del consumo sobre 30 km, detección de recarga, autonomía y riesgo), `track.py` (TrackProcessor: outliers GPS por encima de 200 km/h, alertas por episodio) |
| `processing/` | `enrichment.py` (SilverEnricher), `queries.py` (`BRONZE_POINTS_WITH_GEOFENCES`: LATERAL con `ST_Intersects` y GiST) |
| `reference.py` | `upsert_reference()` y `load_reference()` contra la BD |
| `seed/` | `bogota.py` (3 rutas, 6 geocercas, 5 vehículos, cada uno prueba una regla) y `python -m simon_geo.seed` |
| `simulator/` | Flota simulada con perfiles de conductor, ruido, duplicados y outliers. CLI con `--out` y `--api-url` |
| `api/` | FastAPI con `POST /v1/telemetry` (lotes, idempotente, rechazo por paquete), `GET /v1/alerts` (enmascarado según rol) y `GET /health`. Roles por `X-API-Key` |

### Tests y CI

- `tests/unit/`: **122 tests que pasan** sin BD (reglas, geocercas, combustible, track, API y simulación completa).
- `tests/integration/`: 29 tests que requieren PostGIS. Sin BD se saltan, y con `REQUIRE_DB=1` fallan en su lugar. Cubren:
  - esquema, EPSG:9377 y parámetros;
  - particiones;
  - permisos y enmascaramiento;
  - ingesta idempotente;
  - **geocercas de PostGIS idénticas a las de Python**.
- `.github/workflows/ci.yml`: el job `unit` corre ruff y pytest; el job `integration` hace `docker compose up --wait`, carga el seed y corre `pytest -m integration`.

### Documentación

- `SETUP.md`: completo.
- `README.md`: inicial. **Hay que actualizarlo**, porque no menciona la API, `processing/`, la CI ni el simulador.

### Resultados del escenario Bogotá (en memoria)

| Vehículo | Caso | Resultado |
|---|---|---|
| VHC-8472-ABC | Conductor cumplido, ruta R01 | 0 alertas, 0 eventos bruscos. Consumo estimado 31.9 L/100 km (real 32) |
| VHC-1935-BOG | Agresivo, ruta R02 | Alertas HIGH y CRITICAL en BOG-AUTOSUR |
| VHC-5521-KLM | Moderado, ruta R03 | Alerta MEDIUM en la zona escolar de Teusaquillo |
| VHC-7310-TRZ | Combustible al 3.5 %, ruta R02 | Alerta CRITICAL de combustible. Se queda sin gasolina en el km 13.9 de 15.4 |
| VHC-2048-QWE | Recarga en el km 6, ruta R01 | Recarga detectada y ventana de consumo reiniciada |

## 5. Pendiente, en orden de prioridad

1. ~~Worker Bronce → Plata → Oro~~ **HECHO** (2026-11-20). `processing/worker.py` y `python -m simon_geo.processing`:
   - watermark por `ingested_at` con `FOR UPDATE`; lookback de 2 h para continuar el estado de la traza; escribe desde el primer `event_ts` nuevo (upsert de puntos, borrado y reinserción de alertas del rango, para corregir con datos tardíos); refresco de Oro en transacción aparte.
   - 11 tests unitarios (`tests/unit/test_worker.py`) y 7 de integración (`tests/integration/test_worker.py`). **Los de integración aún no se han ejecutado** (no hay Docker en este PC; los correrá la CI).
2. **Benchmark Big Data** (pesa el 20 %). Pasos:
   - generador masivo (millones de filas) que cargue con `COPY`;
   - `EXPLAIN (ANALYZE, BUFFERS)` de consultas típicas (puntos en geocerca, ventana temporal y vehículo, heatmap), comparando sin y con índices GiST/BRIN y la poda de particiones;
   - guardar los resultados en `docs/benchmark.md`.
3. ~~DESIGN.md~~ **HECHO** (2026-11-20; falta enlazar números reales de `docs/benchmark.md`). Era: arquitectura de capas, modelo de datos, decisiones y trade-offs. Temas a cubrir:
   - por qué se particiona por día;
   - por qué EPSG:9377;
   - Python y PostGIS para las geocercas;
   - enmascaramiento en la BD y no en la API;
   - idempotencia;
   - el modelo de combustible;
   - cómo escalaría (Kafka, Timescale/Citus, etc.).
4. **Dashboard Streamlit** (`dashboard/app.py`): ruta del vehículo, heatmap de infracciones (`gold.infraction_grid_250m`), KPIs y Driver Score. Debe conectarse como `analyst_demo` para mostrar el enmascaramiento.
5. **README.md** actualizado con el diagrama, la estructura y los enlaces.
6. **Guion del video** de 5 minutos: arquitectura, demo de ingesta, demo de enmascaramiento con analista y con admin, geocercas, alertas, EXPLAIN y dashboard.

**Pregunta abierta:** el límite del patio (BOG-PATIO-FONTIBON) está en 20 km/h como política interna. Falta decidir si se deja así o se cambia a 30.

## 6. Decisiones clave (para no re-discutirlas)

- `ST_Intersects`: un punto sobre el borde cuenta como dentro de la geocerca. Python usa la misma semántica.
- La regla de velocidad se resuelve por **prioridad de zona** y no por "límite mínimo global".
- El enmascaramiento ocurre **en PostgreSQL**, mediante vistas `analytics.*` y `security.is_admin()`. La API solo elige el usuario de BD según la API key.
- Las alertas se emiten **una por episodio**, más otra si escala la severidad. No se emite una por punto.
- Se usan tiempos en UTC. Solo los KPI diarios se agrupan por día local en `America/Bogota`.
- En `api/app.py` **no** se usa `from __future__ import annotations`, porque rompe las dependencias de FastAPI creadas en closures (todas devolvían 422).
