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

## 7. Procesar Bronce → Plata → Oro

```bash
python -m simon_geo.processing              # procesa lo pendiente y termina
python -m simon_geo.processing --loop -i 10 # cada 10 s, hasta Ctrl+C
```

Cada lote enriquece lo nuevo de Bronce (geocercas, excesos de velocidad, riesgo de
combustible), escribe `silver.telemetry_enriched` y `silver.alert`, avanza el watermark
y refresca las vistas de `gold`. Al terminar imprime un resumen JSON del lote.
Es seguro ejecutarlo varias veces: lo ya procesado no se repite.

Opciones: `--lookback-min` (historia que se relee para continuar cada traza, 120 por
defecto), `--settle-s` y `--no-gold`.

## 8. Tablero (Streamlit)

```bash
streamlit run dashboard/app.py
```

Abre <http://localhost:8501>. En la barra lateral, **Ver como** cambia el usuario de base
de datos: como *Analista BI* los IDs salen enmascarados (`VHC-****-ABC`), y como
*Administrador* salen reales. El mapa muestra las rutas del día, el mapa de calor y los
puntos de infracción, las geocercas y la malla de 250 m de puntos calientes.

## 9. Benchmark (millones de puntos)

```bash
python -m simon_geo.benchmark                          # ~5 M de puntos, varios minutos
python -m simon_geo.benchmark --vehicles 100 --days 2  # prueba rápida
```

Genera los datos en un esquema aparte (`bench`), con las mismas particiones e índices que
Bronce, y escribe `docs/benchmark.md` con los tiempos y planes de `EXPLAIN ANALYZE`. Para
borrar esos datos: `python -m simon_geo.benchmark --drop`.

## 10. Pruebas

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
