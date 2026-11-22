# PROGRESO · Estado del trabajo y cómo continuar

> Bitácora de traspaso. **Borrarla del repo antes de la entrega** (son notas internas).
> Última actualización: 2026-11-22.

## 1. Contexto

- **Prueba técnica:** Ingeniero GIS / Desarrollador de Datos Espaciales (Simón Movilidad).
- **Pide:** pipeline Bronce → Plata → Oro en PostgreSQL + PostGIS:
  - ingesta con enmascaramiento `VHC-****-ABC`;
  - geocercas;
  - alertas de velocidad por zona y alerta predictiva de combustible;
  - capa Oro con GiST;
  - dashboard opcional.
- **Entregables:** repo público, tests, `DESIGN.md`, `SETUP.md` y un video de 5 minutos como máximo.
- **Repo:** <https://github.com/JuanManceraS9402/PruebaTec> (rama `main`). La CI está en verde.

## 2. Estado: todo el código está hecho

| Bloque | Estado |
|---|---|
| Esquema SQL (`db/init/00–07`) | Particiones diarias, GiST + BRIN, Plata, Oro (KPIs, Driver Score, malla 250 m, **zonas calientes DBSCAN**), seguridad por rol |
| API FastAPI | Ingesta idempotente por lotes, alertas enmascaradas por rol |
| Worker incremental | Watermark por `ingested_at`, lookback de 2 h, refresco de Oro |
| Simulador | **10 vehículos, 6 rutas reales (OSRM)**, perfiles cumplido/moderado/agresivo, ruido, duplicados y outliers |
| Geocercas | **2.257 reales** de datos abiertos (ver §4) |
| Benchmark | 5,04 M de puntos, `docs/benchmark.md` actualizado con las geocercas reales |
| Tablero Streamlit | Mapa (capas, leyenda, ortofoto IDECA híbrida), BI interactivo (ranking clicable, perfil del vehículo, infracciones por zona y en el tiempo), filtro de placas con un clic, créditos al pie |
| Simulador en el tablero | Solo administrador: ruta existente o dos clics en un mapa (OSRM), pipeline real, limpieza con **Borrar simulados** (`sandbox.py`, `routing.py`, `08_sandbox.sql`) |
| Docs | `README.md`, `DESIGN.md`, `SETUP.md` y `docs/benchmark.md` al día, con fuentes y licencias |
| Pruebas | 232 unitarias y 60 de integración. La CI corre las dos contra PostGIS real |

## 3. Cómo levantar todo (este PC, sin Docker)

PostgreSQL 19 + PostGIS 3.7 nativo en el **puerto 5433**. La contraseña está en `.env`, que no se versiona.

```powershell
cd E:\Usuario\Documentos\Movilidad
.venv\Scripts\activate
python -m simon_geo.bootstrap --reset          # base limpia
python -m simon_geo.seed                       # 10 vehículos, 2.257 geocercas, 6 rutas
uvicorn simon_geo.api.app:app                  # otra terminal
python -m simon_geo.simulator --out data/telemetry.jsonl --api-url http://localhost:8000
python -m simon_geo.processing --settle-s 0
streamlit run dashboard/app.py                 # DESDE LA RAÍZ (lee .streamlit/config.toml)
```

- **Pruebas de integración:** necesitan una base limpia (`bootstrap --reset` + `seed`). Con los datos de la demo cargados falla la prueba de geocercas PostGIS = Python, y es lo esperado.
- **Después de correrlas:** regenerar la demo.
- **Simulador:** usa la última jornada ya terminada (hoy o ayer a las 06:00), porque la API rechaza fechas futuras.

## 4. Datos reales y referencias

| Capa | Fuente | Licencia |
|---|---|---|
| Rutas R01–R06 y corredores | OpenStreetMap vía OSRM (`scripts/fetch_osm_routes.py`) | ODbL |
| Perímetro urbano (POT) | Secretaría Distrital de Planeación, datosabiertos.bogota.gov.co | CC BY 4.0 |
| 2.209 zonas escolares (100 m por sede) | Colegios Bogotá D.C. 12/2025, Secretaría de Educación | CC BY-SA 4.0 |
| 44 tramos y puntos críticos de siniestralidad 2022–2025 | Oficina de Seguridad Vial, Secretaría de Movilidad (sig.simur.gov.co) | CC BY 4.0 |
| Ortofoto urbana 2025 | SDP, publicada por IDECA – UAECD (ImageServer `orthourbana2025funcion`) | Servicio público |
| Mapa base | CARTO Positron (vectorial, sin clave) | — |

- **Scripts de datos:** `scripts/fetch_bogota_open_data.py` y `scripts/fetch_osm_routes.py --only R04,...`. Los GeoJSON resultantes están versionados en `src/simon_geo/seed/data/`.
- **Dónde están las referencias:** en el README (tabla "Datos y créditos"), en `DESIGN.md` §3 y en el tablero (desplegable "Fuentes de datos y licencias" al pie).
- **Lo que no se usó:**
  - el histórico de siniestros, porque dejó de ser público;
  - las "zonas críticas en vías locales", porque son áreas enormes;
  - la ortofoto de dron 2024, porque cubre solo 941 ha.

## 5. Números clave (para el video)

- **Driver Score:** los cumplidos sacan 100; los moderados, de 44 a 71; los agresivos, de 1,3 a 9,6.
- **Alertas:** 127 en total, 13 en tramos críticos y 67 en zonas escolares.
- **Benchmark (5,04 M de puntos):**

  | Medida | Con índice | Sin índice |
  |---|---|---|
  | Zona escolar en un día (GiST de puntos) | 0,2 ms | 97 ms |
  | Enriquecimiento de una hora (GiST de geocercas) | 1,05 s | 15,7 s |
  | Oro: KPIs y rutas de 500 vehículos (optimizado) | 27 ms | antes 151 ms |
  | Oro: infracciones en un recuadro del mapa (optimizado) | 2 ms | antes 72 ms |
  | Tamaño del índice de tiempo | BRIN: 224 KB | B-tree: 35 MB |

- **Contraste para la demo:** VHC-6194-PLM (agresivo por Américas y NQS) frente a VHC-9053-HRD (cumplido, 100).

## 6. Pendiente para entregar

0. **Preparar la grabación** con `preparar_video.ps1` (local): deja la base limpia y levanta API y tablero.

1. **Grabar el video** siguiendo `GUION_VIDEO.md`. Es local y está en `.gitignore`; tiene la sección de capas y una tabla de referencia rápida.
2. **Subir el video** como no listado y poner el enlace en el README.
3. **Borrar este `PROGRESO.md`** del repo.
4. **Opcional:** zonas calientes con 5 excesos mínimos en vez de 3. Con 127 alertas hay 17 zonas; quedarían unas 9.

## 7. Decisiones clave (para no re-discutirlas)

**Geocercas y velocidad**
- Se usa `ST_Intersects`: un punto en el borde cuenta como dentro. Python usa la misma semántica.
- El límite se resuelve por **prioridad de zona** (escolar > restringida > siniestralidad > patio > corredor > urbano), no por el mínimo global.
- En zonas escolares y de alta siniestralidad la severidad sube un nivel.
- Se emite **una alerta por episodio**, más otra si la severidad escala.
- Límites: 50 km/h en la ciudad, incluidas autopistas (Ley 2251 de 2022); 30 km/h en zona escolar; 20 km/h en el patio, como política interna; 80 km/h fuera del perímetro.

**Datos y privacidad**
- El enmascaramiento se hace **en PostgreSQL** (vistas `analytics.*`), no en la API ni en el tablero.
- Los tiempos se guardan en UTC. Los KPI diarios se agrupan por día local `America/Bogota`.

**Detalles técnicos que causaron errores**
- En pydeck, todo texto se trata como expresión. Las unidades van con `pdk.types.String("pixels")`; un campo llamado `width` choca con el ancho del lienzo.
- En `api/app.py` **no** se usa `from __future__ import annotations`, porque rompe las dependencias de FastAPI.
