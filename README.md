# Simón Geo · Pipeline geoespacial de telemetría vehicular

Pipeline Bronce → Plata → Oro sobre PostgreSQL + PostGIS para ingerir telemetría IoT/AVL,
enriquecerla con geocercas, generar alertas (exceso de velocidad dinámico y riesgo de
combustible) y exponer KPIs de *Driver Score* para BI.

- **Arquitectura y decisiones:** [DESIGN.md](DESIGN.md)
- **Despliegue local paso a paso:** [SETUP.md](SETUP.md)

## Estructura

```
.
├── docker-compose.yml        # PostGIS 16 + 3.4 con inicialización automática
├── db/init/                  # Scripts SQL ejecutados al crear la base
│   ├── 00_extensions.sql     # PostGIS, pg_stat_statements, EPSG:9377
│   ├── 01_schemas_ops.sql    # Esquemas por capa, parámetros, particionado
│   ├── 02_reference.sql      # Vehículos, geocercas, rutas
│   ├── 03_bronze.sql         # Telemetría cruda particionada por día
│   ├── 04_silver.sql         # Telemetría enriquecida + alertas
│   ├── 05_gold.sql           # Vistas materializadas para BI + Driver Score
│   ├── 06_security.sql       # Roles, enmascaramiento, esquema analytics
│   └── 07_users.sh           # Usuarios de login
├── src/simon_geo/            # Código Python (ingesta, procesamiento, dashboard)
└── tests/                    # Unitarios + integración
```

## Modelo de acceso

| Usuario        | Rol                | Puede                                                     |
|----------------|--------------------|-----------------------------------------------------------|
| `api_ingest`   | `telemetry_ingest` | Insertar en `bronze.telemetry_raw`                        |
| `etl_worker`   | `pipeline_worker`  | Leer Bronce, escribir Plata, refrescar Oro                |
| `analyst_demo` | `bi_analyst`       | Leer solo `analytics.*` con IDs enmascarados (`VHC-****-ABC`) |
| `admin_demo`   | `fleet_admin`      | Leer todas las capas, IDs reales                          |
