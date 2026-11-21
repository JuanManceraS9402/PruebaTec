# Benchmark · consultas espaciales sobre millones de puntos GPS

> Generado con `python -m simon_geo.benchmark`. Los números dependen de la máquina;
> lo relevante es la **diferencia entre variantes** y el **plan** que elige PostgreSQL.

## Datos

* **5,040,000 puntos**: 500 vehículos × 7 días × 1 reporte cada 60 s, en Bogotá.
* Tabla `bench.telemetry_raw`: misma definición, particiones diarias e índices que `bronze.telemetry_raw`.
* Carga (`INSERT … SELECT`, con índices activos, primera ejecución): 114,7 s → **~44.000 filas/s**.
* Interpretación de los resultados: [DESIGN.md §6](../DESIGN.md#6-estrategia-de-índices).

| Entorno | |
|---|---|
| PostgreSQL | 19.0 |
| PostGIS | 3.7.0 |
| shared_buffers | 128MB |
| work_mem | 4MB |
| CPU (cliente) | 12 núcleos lógicos |
| SO (cliente) | Windows 10 |
| Fecha | 2026-11-21 01:07 UTC |

## Resumen

| Consulta | Variante | Tiempo (mediana) | Páginas leídas | Particiones | Acceso |
|---|---|---:|---:|---:|---|
| Puntos dentro de una geocerca en un día | Con índices (GiST + BRIN + poda) | 0.4 ms | 274 | 1 | Index Scan (geofence_code_key)<br>Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_geom_gist) |
|  | Sin GiST | 102.8 ms | 11,079 | 1 | Index Scan (geofence_code_key)<br>Seq Scan |
|  | Sin filtro de tiempo (7 días, sin poda) | 59.0 ms | 1,919 | 7 | Index Scan (geofence_code_key)<br>Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_geom_gist) |
| Viewport del mapa en una hora | Con índices | 5.1 ms | 328 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_event_brin)<br>Bitmap Index Scan (telemetry_raw_geom_gist) |
|  | Sin GiST | 7.8 ms | 488 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_event_brin) |
|  | Sin BRIN | 9.4 ms | 4,555 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_geom_gist)<br>Bitmap Index Scan (telemetry_raw_pkey) |
| Traza de un vehículo en un día | Con índices (PK + poda) | 1.3 ms | 1,455 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_pkey) |
| Enriquecimiento de geocercas de una hora (consulta del worker) | Con índices | 53.9 ms | 30,493 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_event_brin)<br>Index Scan (geofence_geom_gist) |
| Malla de calor de 250 m de excesos en un día | Un día (poda a 1 partición) | 262.0 ms | 12,529 | 1 | Seq Scan |
|  | 7 días (sin poda) | 1,443.6 ms | 78,991 | 7 | Seq Scan |

## Tamaño de la tabla y sus índices

| Objeto | Tamaño |
|---|---:|
| `Tabla (heap)` | 605.8 MB |
| `telemetry_raw_pkey` | 334.4 MB |
| `telemetry_raw_geom_gist` | 202.2 MB |
| `telemetry_raw_event_brin` | 224 KB |
| `telemetry_raw_ingested_brin` | 224 KB |
| *B-tree equivalente sobre `event_ts` (creado y descartado)* | 35.3 MB |

## Planes de ejecución

### Puntos dentro de una geocerca en un día

¿Cuántos reportes hubo dentro de la zona escolar de Teusaquillo ayer?

```sql
SELECT count(*) FROM bench.telemetry_raw b
        WHERE ST_Intersects(b.geom, (SELECT geom FROM ref.geofence WHERE code = 'BOG-ESC-TEUSAQUILLO')) AND b.event_ts >= '2026-09-25' AND b.event_ts < '2026-09-26'
```

<details>
<summary>Con índices (GiST + BRIN + poda) · 0.4 ms</summary>

```
Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=274
  InitPlan expr_1
    ->  Index Scan using geofence_code_key on geofence (actual rows=1.00 loops=1)
          Index Cond: (code = 'BOG-ESC-TEUSAQUILLO'::text)
          Index Searches: 1
          Buffers: shared hit=2
  ->  Bitmap Heap Scan on telemetry_raw_p20260925 b (actual rows=186.00 loops=1)
        Filter: ((event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone) AND st_intersects(geom, (InitPlan expr_1).col1))
        Rows Removed by Filter: 78
        Heap Blocks: exact=264
        Buffers: shared hit=274
        ->  Bitmap Index Scan on telemetry_raw_p20260925_geom_idx (actual rows=264.00 loops=1)
              Index Cond: (geom && (InitPlan expr_1).col1)
              Index Searches: 1
              Buffers: shared hit=10
Planning:
  Buffers: shared hit=1
Planning Time: 0.161 ms
Execution Time: 0.396 ms
```
</details>

<details>
<summary>Sin GiST · 102.8 ms</summary>

```
Finalize Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=11079
  InitPlan expr_1
    ->  Index Scan using geofence_code_key on geofence (actual rows=1.00 loops=1)
          Index Cond: (code = 'BOG-ESC-TEUSAQUILLO'::text)
          Index Searches: 1
          Buffers: shared hit=2
  ->  Gather (actual rows=3.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=11079
        ->  Partial Aggregate (actual rows=1.00 loops=3)
              Buffers: shared hit=11077
              ->  Parallel Seq Scan on telemetry_raw_p20260925 b (actual rows=62.00 loops=3)
                    Filter: ((event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone) AND st_intersects(geom, (InitPlan expr_1).col1))
                    Rows Removed by Filter: 239938
                    Buffers: shared hit=11077
Planning:
  Buffers: shared hit=1
Planning Time: 0.173 ms
Execution Time: 91.746 ms
```
</details>

<details>
<summary>Sin filtro de tiempo (7 días, sin poda) · 59.0 ms</summary>

```
Finalize Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=1919
  InitPlan expr_1
    ->  Index Scan using geofence_code_key on geofence (actual rows=1.00 loops=1)
          Index Cond: (code = 'BOG-ESC-TEUSAQUILLO'::text)
          Index Searches: 1
          Buffers: shared hit=2
  ->  Gather (actual rows=3.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=1919
        ->  Partial Aggregate (actual rows=1.00 loops=3)
              Buffers: shared hit=1917
              ->  Parallel Append (actual rows=435.00 loops=3)
                    Buffers: shared hit=1917
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260922 b_1 (actual rows=187.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 81
                          Heap Blocks: exact=268
                          Buffers: shared hit=275
                          ->  Bitmap Index Scan on telemetry_raw_p20260922_geom_idx (actual rows=268.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=7
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260923 b_2 (actual rows=180.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 82
                          Heap Blocks: exact=262
                          Buffers: shared hit=270
                          ->  Bitmap Index Scan on telemetry_raw_p20260923_geom_idx (actual rows=262.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=8
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260924 b_3 (actual rows=184.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 81
                          Heap Blocks: exact=265
                          Buffers: shared hit=275
                          ->  Bitmap Index Scan on telemetry_raw_p20260924_geom_idx (actual rows=265.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=10
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260925 b_4 (actual rows=186.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 78
                          Heap Blocks: exact=264
                          Buffers: shared hit=272
                          ->  Bitmap Index Scan on telemetry_raw_p20260925_geom_idx (actual rows=264.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=8
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260926 b_5 (actual rows=188.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 81
                          Heap Blocks: exact=269
                          Buffers: shared hit=275
                          ->  Bitmap Index Scan on telemetry_raw_p20260926_geom_idx (actual rows=269.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=6
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260927 b_6 (actual rows=188.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 80
                          Heap Blocks: exact=268
                          Buffers: shared hit=278
                          ->  Bitmap Index Scan on telemetry_raw_p20260927_geom_idx (actual rows=268.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=10
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260928 b_7 (actual rows=192.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 75
                          Heap Blocks: exact=267
                          Buffers: shared hit=272
                          ->  Bitmap Index Scan on telemetry_raw_p20260928_geom_idx (actual rows=267.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=5
Planning Time: 0.418 ms
Execution Time: 56.120 ms
```
</details>

### Viewport del mapa en una hora

¿Qué vehículos pasaron por este recuadro del mapa entre las 10:00 y las 11:00?

```sql
SELECT b.vehicle_id, b.event_ts, b.speed_kmh FROM bench.telemetry_raw b
        WHERE b.geom && ST_MakeEnvelope(-74.09, 4.63, -74.06, 4.66, 4326) AND b.event_ts >= '2026-09-25 15:00+00' AND b.event_ts < '2026-09-25 16:00+00'
```

<details>
<summary>Con índices · 5.1 ms</summary>

```
Bitmap Heap Scan on telemetry_raw_p20260925 b (actual rows=319.00 loops=1)
  Recheck Cond: ((event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone) AND (geom && '0103000020E61000000100000005000000F6285C8FC28552C085EB51B81E851240F6285C8FC28552C0A4703D0AD7A31240A4703D0AD78352C0A4703D0AD7A31240A4703D0AD78352C085EB51B81E851240F6285C8FC28552C085EB51B81E851240'::geometry))
  Rows Removed by Index Recheck: 15796
  Heap Blocks: lossy=248
  Buffers: shared hit=328
  ->  BitmapAnd (actual rows=0.00 loops=1)
        Buffers: shared hit=80
        ->  Bitmap Index Scan on telemetry_raw_p20260925_event_ts_idx (actual rows=4850.00 loops=1)
              Index Cond: ((event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone))
              Index Searches: 1
              Buffers: shared hit=3
        ->  Bitmap Index Scan on telemetry_raw_p20260925_geom_idx (actual rows=10690.00 loops=1)
              Index Cond: (geom && '0103000020E61000000100000005000000F6285C8FC28552C085EB51B81E851240F6285C8FC28552C0A4703D0AD7A31240A4703D0AD78352C0A4703D0AD7A31240A4703D0AD78352C085EB51B81E851240F6285C8FC28552C085EB51B81E851240'::geometry)
              Index Searches: 1
              Buffers: shared hit=77
Planning:
  Buffers: shared hit=1
Planning Time: 0.360 ms
Execution Time: 5.555 ms
```
</details>

<details>
<summary>Sin GiST · 7.8 ms</summary>

```
Bitmap Heap Scan on telemetry_raw_p20260925 b (actual rows=319.00 loops=1)
  Recheck Cond: ((event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone))
  Rows Removed by Index Recheck: 1520
  Filter: (geom && '0103000020E61000000100000005000000F6285C8FC28552C085EB51B81E851240F6285C8FC28552C0A4703D0AD7A31240A4703D0AD78352C0A4703D0AD7A31240A4703D0AD78352C085EB51B81E851240F6285C8FC28552C085EB51B81E851240'::geometry)
  Rows Removed by Filter: 29681
  Heap Blocks: lossy=485
  Buffers: shared hit=488
  ->  Bitmap Index Scan on telemetry_raw_p20260925_event_ts_idx (actual rows=4850.00 loops=1)
        Index Cond: ((event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone))
        Index Searches: 1
        Buffers: shared hit=3
Planning:
  Buffers: shared hit=1
Planning Time: 0.208 ms
Execution Time: 7.229 ms
```
</details>

<details>
<summary>Sin BRIN · 9.4 ms</summary>

```
Bitmap Heap Scan on telemetry_raw_p20260925 b (actual rows=319.00 loops=1)
  Recheck Cond: ((geom && '0103000020E61000000100000005000000F6285C8FC28552C085EB51B81E851240F6285C8FC28552C0A4703D0AD7A31240A4703D0AD78352C0A4703D0AD7A31240A4703D0AD78352C085EB51B81E851240F6285C8FC28552C085EB51B81E851240'::geometry) AND (event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone))
  Heap Blocks: exact=233
  Buffers: shared hit=4555
  ->  BitmapAnd (actual rows=0.00 loops=1)
        Buffers: shared hit=4322
        ->  Bitmap Index Scan on telemetry_raw_p20260925_geom_idx (actual rows=10690.00 loops=1)
              Index Cond: (geom && '0103000020E61000000100000005000000F6285C8FC28552C085EB51B81E851240F6285C8FC28552C0A4703D0AD7A31240A4703D0AD78352C0A4703D0AD7A31240A4703D0AD78352C085EB51B81E851240F6285C8FC28552C085EB51B81E851240'::geometry)
              Index Searches: 1
              Buffers: shared hit=77
        ->  Bitmap Index Scan on telemetry_raw_p20260925_pkey (actual rows=30000.00 loops=1)
              Index Cond: ((event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone))
              Index Searches: 1001
              Buffers: shared hit=4245
Planning Time: 0.167 ms
Execution Time: 8.629 ms
```
</details>

### Traza de un vehículo en un día

Ruta recorrida por un vehículo en un día (capa de línea del tablero).

```sql
SELECT b.event_ts, b.geom, b.speed_kmh FROM bench.telemetry_raw b
        WHERE b.vehicle_id = 'BEN-0042-BEN' AND b.event_ts >= '2026-09-25' AND b.event_ts < '2026-09-26'
        ORDER BY b.event_ts
```

<details>
<summary>Con índices (PK + poda) · 1.3 ms</summary>

```
Sort (actual rows=1440.00 loops=1)
  Sort Key: b.event_ts
  Sort Method: quicksort  Memory: 139kB
  Buffers: shared hit=1455
  ->  Bitmap Heap Scan on telemetry_raw_p20260925 b (actual rows=1440.00 loops=1)
        Recheck Cond: ((vehicle_id = 'BEN-0042-BEN'::text) AND (event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone))
        Heap Blocks: exact=1440
        Buffers: shared hit=1455
        ->  Bitmap Index Scan on telemetry_raw_p20260925_pkey (actual rows=1440.00 loops=1)
              Index Cond: ((vehicle_id = 'BEN-0042-BEN'::text) AND (event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone))
              Index Searches: 1
              Buffers: shared hit=15
Planning:
  Buffers: shared hit=1
Planning Time: 0.097 ms
Execution Time: 0.879 ms
```
</details>

### Enriquecimiento de geocercas de una hora (consulta del worker)

Geocercas de cada punto con LEFT JOIN LATERAL + ST_Intersects.

```sql
SELECT count(*), sum(cardinality(gf.geofence_ids)) FROM bench.telemetry_raw b
        LEFT JOIN LATERAL (
            SELECT array_agg(g.geofence_id) AS geofence_ids FROM ref.geofence g
            WHERE g.active AND ST_Intersects(g.geom, b.geom)
        ) gf ON true
        WHERE b.event_ts >= '2026-09-25 15:00+00' AND b.event_ts < '2026-09-25 16:00+00'
```

<details>
<summary>Con índices · 53.9 ms</summary>

```
Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=30493
  ->  Nested Loop Left Join (actual rows=30000.00 loops=1)
        Buffers: shared hit=30493
        ->  Bitmap Heap Scan on telemetry_raw_p20260925 b (actual rows=30000.00 loops=1)
              Recheck Cond: ((event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone))
              Rows Removed by Index Recheck: 1520
              Heap Blocks: lossy=485
              Buffers: shared hit=488
              ->  Bitmap Index Scan on telemetry_raw_p20260925_event_ts_idx (actual rows=4850.00 loops=1)
                    Index Cond: ((event_ts >= '2026-09-25 15:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-25 16:00:00+00'::timestamp with time zone))
                    Index Searches: 1
                    Buffers: shared hit=3
        ->  Aggregate (actual rows=1.00 loops=30000)
              Buffers: shared hit=30005
              ->  Index Scan using geofence_geom_gist on geofence g (actual rows=0.55 loops=30000)
                    Index Cond: (geom && b.geom)
                    Filter: st_intersects(geom, b.geom)
                    Rows Removed by Filter: 0
                    Index Searches: 30000
                    Buffers: shared hit=30005
Planning:
  Buffers: shared hit=1
Planning Time: 0.231 ms
Execution Time: 53.561 ms
```
</details>

### Malla de calor de 250 m de excesos en un día

Agregación en EPSG:9377 de los puntos a más de 50 km/h.

```sql
SELECT floor(ST_X(p) / 250) AS cx, floor(ST_Y(p) / 250) AS cy, count(*)
        FROM (
            SELECT ST_Transform(b.geom, 9377) AS p FROM bench.telemetry_raw b
            WHERE b.speed_kmh > 50 AND b.event_ts >= '2026-09-25' AND b.event_ts < '2026-09-26'
        ) s
        GROUP BY 1, 2
```

<details>
<summary>Un día (poda a 1 partición) · 262.0 ms</summary>

```
GroupAggregate (actual rows=9588.00 loops=1)
  Group Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
  Buffers: shared hit=12529, temp read=315 written=316
  ->  Gather Merge (actual rows=239701.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=12529, temp read=315 written=316
        ->  Sort (actual rows=79900.33 loops=3)
              Sort Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
              Sort Method: external merge  Disk: 2520kB
              Buffers: shared hit=12529, temp read=315 written=316
              Worker 0:  Sort Method: quicksort  Memory: 3933kB
              Worker 1:  Sort Method: quicksort  Memory: 3990kB
              ->  Parallel Seq Scan on telemetry_raw_p20260925 b (actual rows=79900.33 loops=3)
                    Filter: ((speed_kmh > '50'::double precision) AND (event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone))
                    Rows Removed by Filter: 160100
                    Buffers: shared hit=12453
Planning:
  Buffers: shared hit=1
Planning Time: 0.200 ms
Execution Time: 232.842 ms
```
</details>

<details>
<summary>7 días (sin poda) · 1,443.6 ms</summary>

```
GroupAggregate (actual rows=9897.00 loops=1)
  Group Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
  Buffers: shared hit=14423 read=64568 written=8, temp read=5341 written=5363
  ->  Gather Merge (actual rows=1678376.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=14423 read=64568 written=8, temp read=5341 written=5363
        ->  Sort (actual rows=559458.67 loops=3)
              Sort Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
              Sort Method: external merge  Disk: 14776kB
              Buffers: shared hit=14423 read=64568 written=8, temp read=5341 written=5363
              Worker 0:  Sort Method: external merge  Disk: 14392kB
              Worker 1:  Sort Method: external merge  Disk: 13560kB
              ->  Parallel Append (actual rows=559458.67 loops=3)
                    Buffers: shared hit=14347 read=64568 written=8
                    ->  Parallel Seq Scan on telemetry_raw_p20260927 b_6 (actual rows=239718.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480282
                          Buffers: shared hit=1313 read=10452
                    ->  Parallel Seq Scan on telemetry_raw_p20260926 b_5 (actual rows=239896.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480104
                          Buffers: shared hit=1330 read=10435
                    ->  Parallel Seq Scan on telemetry_raw_p20260928 b_7 (actual rows=239858.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480142
                          Buffers: shared hit=643 read=10434
                    ->  Parallel Seq Scan on telemetry_raw_p20260925 b_4 (actual rows=239701.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480299
                          Buffers: shared hit=8560 read=2517
                    ->  Parallel Seq Scan on telemetry_raw_p20260923 b_2 (actual rows=79910.33 loops=3)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 160090
                          Buffers: shared hit=1310 read=9767 written=8
                    ->  Parallel Seq Scan on telemetry_raw_p20260924 b_3 (actual rows=239745.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480255
                          Buffers: shared hit=641 read=10436
                    ->  Parallel Seq Scan on telemetry_raw_p20260922 b_1 (actual rows=239727.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480273
                          Buffers: shared hit=550 read=10527
Planning Time: 0.416 ms
Execution Time: 1325.687 ms
```
</details>

