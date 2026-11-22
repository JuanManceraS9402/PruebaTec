# Benchmark de la capa Oro

> Generado con `python -m simon_geo.benchmark.gold`. Mide lo que espera el tablero:
> consulta más transferencia de filas, como `bi_analyst` y a través de las vistas
> enmascaradas. Los números dependen de la máquina.

* **5,040,000 puntos** en Plata (500 vehículos × 7 días, un reporte cada 60 s) y **125,625 alertas**.
* PostgreSQL 19.0, PostGIS 3.7.0, configuración por defecto (`shared_buffers` 128MB, `work_mem` 4MB).
* Fecha: 2026-11-22 15:22 UTC.

## Consultas del tablero

| Consulta | Tiempo (mediana) | Filas | Datos | < 100 ms |
|---|---:|---:|---:|:---:|
| Días disponibles (selector) | **2.9 ms** | 8 | 0.00 MB | sí |
| KPIs y rutas del día, toda la flota | **26.6 ms** | 500 | 3.70 MB | sí |
| Infracciones del día | **65.3 ms** | 17,977 | 2.55 MB | sí |
| Infracciones de un día en el recuadro del mapa (GiST) | **2.0 ms** | 250 | 0.02 MB | sí |
| Top 10 peores conductores del día | **1.5 ms** | 10 | 0.00 MB | sí |
| Zonas calientes | **0.3 ms** | 1 | 0.00 MB | sí |
| Malla de calor de 250 m | **9.8 ms** | 8,931 | 0.09 MB | sí |

## Construcción de Oro (refresco completo)

| Vista | Tiempo |
|---|---:|
| `gold.vehicle_daily_kpi` | 9.3 s |
| `gold.infraction_point` | 1.2 s |
| `gold.infraction_grid_250m` | 0.3 s |
| `gold.infraction_hotspot` | 1.4 s |
| **Total** | **12.2 s** |

## Cómo leerlo

* Los datos son sintéticos. Sus alertas cubren la ciudad de forma casi uniforme, así
  que DBSCAN (3 excesos a menos de 600 m) las une en muy pocas zonas: el tiempo de
  *Zonas calientes* no representa un caso con muchas zonas.
* Las consultas leen vistas materializadas: el costo se paga al refrescar, no al leer.
  El refresco es completo y crece con la historia (ver DESIGN.md, sección 8).
