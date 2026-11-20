# SETUP · Despliegue local

Requisitos: **Docker** (con Docker Compose v2) y **Python 3.11+**.

## 1. Configuración

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Los valores por defecto sirven para desarrollo local. En cualquier otro entorno cambia
todas las contraseñas y API keys.

## 2. Base de datos (PostGIS)

```bash
docker compose up -d --wait
```

Al crear el volumen por primera vez, el contenedor ejecuta en orden los scripts de `db/init/`:

| Script | Qué hace |
|---|---|
| `00_extensions.sql` | PostGIS, `pg_stat_statements`, zona horaria UTC, EPSG:9377 |
| `01_schemas_ops.sql` | Esquemas por capa, parámetros de negocio, función de particionado |
| `02_reference.sql` | Tablas maestras (vehículos, geocercas, rutas) con índices GiST |
| `03_bronze.sql` | Telemetría cruda particionada por día |
| `04_silver.sql` | Telemetría enriquecida y alertas |
| `05_gold.sql` | Vistas materializadas para BI y función de Driver Score |
| `06_security.sql` | Roles, enmascaramiento y esquema `analytics` |
| `07_users.sh` | Usuarios de login con las contraseñas del `.env` |

`--wait` retorna cuando el healthcheck responde por TCP, es decir, cuando los scripts
ya terminaron.

> Los scripts solo se ejecutan con un volumen vacío. Para reinicializar desde cero:
> `docker compose down -v && docker compose up -d --wait`

## 3. Entorno Python

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows:     .venv\Scripts\activate
pip install -e ".[dev,dashboard]"
```

## 4. Datos maestros del escenario Bogotá

```bash
python -m simon_geo.seed
```

Carga 5 vehículos, 6 geocercas (perímetro urbano, corredores, alta siniestralidad,
zona escolar y patio), 3 rutas y la asignación de ruta de cada vehículo. Es idempotente.

## 5. API de ingesta

```bash
uvicorn simon_geo.api.app:app --reload
```

Documentación interactiva en <http://localhost:8000/docs>.

## 6. Simular la flota enviando a la API

En otra terminal:

```bash
python -m simon_geo.simulator --out data/telemetry.jsonl --api-url http://localhost:8000
```

## 7. Pruebas

```bash
pytest -m "not integration"   # unitarias: no requieren base de datos
pytest -m integration         # requieren los pasos 2 y 4
```

Sin base de datos, los tests de integración se saltan. En CI (`.github/workflows/ci.yml`)
se ejecutan contra el mismo `docker compose` con `REQUIRE_DB=1`, que los hace fallar
en lugar de saltarse.

## Usuarios de base de datos

| Usuario | Contraseña por defecto | Uso |
|---|---|---|
| `postgres` | `postgres_dev` | Dueño del esquema; solo para el seed |
| `api_ingest` | `ingest_dev` | API de ingesta |
| `etl_worker` | `etl_dev` | Procesamiento Bronce → Plata → Oro |
| `analyst_demo` | `analyst_dev` | Analista BI: IDs enmascarados |
| `admin_demo` | `admin_dev` | Administrador: IDs reales |

Para ver el enmascaramiento en acción:

```bash
docker compose exec db psql -U analyst_demo -d simon_geo \
  -c "SELECT vehicle_id, vehicle_key, event_ts FROM analytics.telemetry_raw LIMIT 3"
```
