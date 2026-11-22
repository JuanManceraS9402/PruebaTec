# Benchmark · consultas espaciales sobre millones de puntos GPS

> Generado con `python -m gps_flota.benchmark`. Los números dependen de la máquina;
> lo relevante es la **diferencia entre variantes** y el **plan** que elige PostgreSQL.

## Datos

* **5,040,000 puntos**: 500 vehículos × 7 días × 1 reporte cada 60 s, en Bogotá.
* Tabla `bench.telemetry_raw`: misma definición, particiones diarias e índices que `bronze.telemetry_raw`.
* Carga (`INSERT … SELECT`, con índices activos): 117.2 s → **43,012 filas/s**.

| Entorno | |
|---|---|
| PostgreSQL | 19.0 |
| PostGIS | 3.7.0 |
| shared_buffers | 128MB |
| work_mem | 4MB |
| CPU (cliente) | 12 núcleos lógicos |
| SO (cliente) | Windows 10 |
| Fecha | 2026-11-21 05:26 UTC |

## Resumen

| Consulta | Variante | Tiempo (mediana) | Páginas leídas | Particiones | Acceso |
|---|---|---:|---:|---:|---|
| Puntos dentro de una geocerca en un día | Con índices (GiST + BRIN + poda) | 0.2 ms | 34 | 1 | Index Scan (geofence_code_key)<br>Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_geom_gist) |
|  | Sin GiST | 97.1 ms | 11,080 | 1 | Index Scan (geofence_code_key)<br>Seq Scan |
|  | Sin filtro de tiempo (7 días, sin poda) | 56.0 ms | 199 | 7 | Index Scan (geofence_code_key)<br>Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_geom_gist) |
| Viewport del mapa en una hora | Con índices | 4.2 ms | 328 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_event_brin)<br>Bitmap Index Scan (telemetry_raw_geom_gist) |
|  | Sin GiST | 5.6 ms | 488 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_event_brin) |
|  | Sin BRIN | 9.6 ms | 4,555 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_geom_gist)<br>Bitmap Index Scan (telemetry_raw_pkey) |
| Traza de un vehículo en un día | Con índices (PK + poda) | 1.0 ms | 1,455 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_pkey) |
| Enriquecimiento de geocercas de una hora (consulta del worker) | Con índices | 1,053.8 ms | 141,987 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_event_brin)<br>Index Scan (geofence_geom_gist) |
|  | Sin GiST de geocercas | 15.7 s | 7,560,493 | 1 | Bitmap Heap Scan<br>Bitmap Index Scan (telemetry_raw_event_brin)<br>Seq Scan |
| Malla de calor de 250 m de excesos en un día | Un día (poda a 1 partición) | 361.8 ms | 12,527 | 1 | Seq Scan |
|  | 7 días (sin poda) | 2,740.7 ms | 78,989 | 7 | Seq Scan |

## Tamaño de la tabla y sus índices

| Objeto | Tamaño |
|---|---:|
| `Tabla (heap)` | 605.8 MB |
| `telemetry_raw_pkey` | 334.4 MB |
| `telemetry_raw_geom_gist` | 202.4 MB |
| `telemetry_raw_event_brin` | 224 KB |
| `telemetry_raw_ingested_brin` | 224 KB |
| *B-tree equivalente sobre `event_ts` (creado y descartado)* | 35.3 MB |

## Planes de ejecución

### Puntos dentro de una geocerca en un día

¿Cuántos reportes hubo ayer dentro de una zona escolar de Teusaquillo?

```sql
SELECT count(*) FROM bench.telemetry_raw b
        WHERE ST_Intersects(b.geom, (SELECT geom FROM ref.geofence WHERE code = 'ESC-311001033536-A')) AND b.event_ts >= '2026-09-25' AND b.event_ts < '2026-09-26'
```

<details>
<summary>Con índices (GiST + BRIN + poda) · 0.2 ms</summary>

```
Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=34
  InitPlan expr_1
    ->  Index Scan using geofence_code_key on geofence (actual rows=1.00 loops=1)
          Index Cond: (code = 'ESC-311001033536-A'::text)
          Index Searches: 1
          Buffers: shared hit=3
  ->  Bitmap Heap Scan on telemetry_raw_p20260925 b (actual rows=24.00 loops=1)
        Filter: ((event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone) AND st_intersects(geom, (InitPlan expr_1).col1))
        Heap Blocks: exact=24
        Buffers: shared hit=34
        ->  Bitmap Index Scan on telemetry_raw_p20260925_geom_idx (actual rows=24.00 loops=1)
              Index Cond: (geom && (InitPlan expr_1).col1)
              Index Searches: 1
              Buffers: shared hit=10
Planning:
  Buffers: shared hit=1
Planning Time: 0.198 ms
Execution Time: 0.166 ms
```
</details>

<details>
<summary>Sin GiST · 97.1 ms</summary>

```
Finalize Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=1343 read=9737
  InitPlan expr_1
    ->  Index Scan using geofence_code_key on geofence (actual rows=1.00 loops=1)
          Index Cond: (code = 'ESC-311001033536-A'::text)
          Index Searches: 1
          Buffers: shared hit=3
  ->  Gather (actual rows=3.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=1343 read=9737
        ->  Partial Aggregate (actual rows=1.00 loops=3)
              Buffers: shared hit=1340 read=9737
              ->  Parallel Seq Scan on telemetry_raw_p20260925 b (actual rows=8.00 loops=3)
                    Filter: ((event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone) AND st_intersects(geom, (InitPlan expr_1).col1))
                    Rows Removed by Filter: 239992
                    Buffers: shared hit=1340 read=9737
Planning:
  Buffers: shared hit=1
Planning Time: 0.195 ms
Execution Time: 102.509 ms
```
</details>

<details>
<summary>Sin filtro de tiempo (7 días, sin poda) · 56.0 ms</summary>

```
Finalize Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=199
  InitPlan expr_1
    ->  Index Scan using geofence_code_key on geofence (actual rows=1.00 loops=1)
          Index Cond: (code = 'ESC-311001033536-A'::text)
          Index Searches: 1
          Buffers: shared hit=3
  ->  Gather (actual rows=3.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=199
        ->  Partial Aggregate (actual rows=1.00 loops=3)
              Buffers: shared hit=196
              ->  Parallel Append (actual rows=51.67 loops=3)
                    Buffers: shared hit=196
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260922 b_1 (actual rows=20.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 4
                          Heap Blocks: exact=24
                          Buffers: shared hit=27
                          ->  Bitmap Index Scan on telemetry_raw_p20260922_geom_idx (actual rows=24.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=3
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260923 b_2 (actual rows=21.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 3
                          Heap Blocks: exact=24
                          Buffers: shared hit=27
                          ->  Bitmap Index Scan on telemetry_raw_p20260923_geom_idx (actual rows=24.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=3
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260924 b_3 (actual rows=21.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Heap Blocks: exact=21
                          Buffers: shared hit=26
                          ->  Bitmap Index Scan on telemetry_raw_p20260924_geom_idx (actual rows=21.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=5
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260925 b_4 (actual rows=24.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Heap Blocks: exact=24
                          Buffers: shared hit=31
                          ->  Bitmap Index Scan on telemetry_raw_p20260925_geom_idx (actual rows=24.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=7
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260926 b_5 (actual rows=24.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Heap Blocks: exact=24
                          Buffers: shared hit=27
                          ->  Bitmap Index Scan on telemetry_raw_p20260926_geom_idx (actual rows=24.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=3
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260927 b_6 (actual rows=21.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Heap Blocks: exact=21
                          Buffers: shared hit=27
                          ->  Bitmap Index Scan on telemetry_raw_p20260927_geom_idx (actual rows=21.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=6
                    ->  Parallel Bitmap Heap Scan on telemetry_raw_p20260928 b_7 (actual rows=24.00 loops=1)
                          Filter: st_intersects(geom, (InitPlan expr_1).col1)
                          Rows Removed by Filter: 1
                          Heap Blocks: exact=25
                          Buffers: shared hit=31
                          ->  Bitmap Index Scan on telemetry_raw_p20260928_geom_idx (actual rows=25.00 loops=1)
                                Index Cond: (geom && (InitPlan expr_1).col1)
                                Index Searches: 1
                                Buffers: shared hit=6
Planning Time: 0.472 ms
Execution Time: 56.061 ms
```
</details>

### Viewport del mapa en una hora

¿Qué vehículos pasaron por este recuadro del mapa entre las 10:00 y las 11:00?

```sql
SELECT b.vehicle_id, b.event_ts, b.speed_kmh FROM bench.telemetry_raw b
        WHERE b.geom && ST_MakeEnvelope(-74.09, 4.63, -74.06, 4.66, 4326) AND b.event_ts >= '2026-09-25 15:00+00' AND b.event_ts < '2026-09-25 16:00+00'
```

<details>
<summary>Con índices · 4.2 ms</summary>

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
Planning Time: 0.169 ms
Execution Time: 4.177 ms
```
</details>

<details>
<summary>Sin GiST · 5.6 ms</summary>

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
Planning Time: 0.166 ms
Execution Time: 5.669 ms
```
</details>

<details>
<summary>Sin BRIN · 9.6 ms</summary>

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
Planning Time: 0.181 ms
Execution Time: 9.136 ms
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
<summary>Con índices (PK + poda) · 1.0 ms</summary>

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
Planning Time: 0.131 ms
Execution Time: 1.033 ms
```
</details>

### Enriquecimiento de geocercas de una hora (consulta del worker)

Geocercas de cada punto con LEFT JOIN LATERAL + ST_Intersects (~2.250 geocercas reales).

```sql
SELECT count(*), sum(cardinality(gf.geofence_ids)) FROM bench.telemetry_raw b
        LEFT JOIN LATERAL (
            SELECT array_agg(g.geofence_id) AS geofence_ids FROM ref.geofence g
            WHERE g.active AND ST_Intersects(g.geom, b.geom)
        ) gf ON true
        WHERE b.event_ts >= '2026-09-25 15:00+00' AND b.event_ts < '2026-09-25 16:00+00'
```

<details>
<summary>Con índices · 1,053.8 ms</summary>

```
Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=141987
  ->  Nested Loop Left Join (actual rows=30000.00 loops=1)
        Buffers: shared hit=141987
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
              Buffers: shared hit=141499
              ->  Index Scan using geofence_geom_gist on geofence g (actual rows=0.58 loops=30000)
                    Index Cond: (geom && b.geom)
                    Filter: st_intersects(geom, b.geom)
                    Rows Removed by Filter: 1
                    Index Searches: 30000
                    Buffers: shared hit=141499
Planning:
  Buffers: shared hit=1
Planning Time: 0.218 ms
Execution Time: 1065.589 ms
```
</details>

<details>
<summary>Sin GiST de geocercas · 15.7 s</summary>

```
Aggregate (actual rows=1.00 loops=1)
  Buffers: shared hit=7560493
  ->  Nested Loop Left Join (actual rows=30000.00 loops=1)
        Buffers: shared hit=7560493
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
              Buffers: shared hit=7560005
              ->  Seq Scan on geofence g (actual rows=0.58 loops=30000)
                    Filter: (active AND st_intersects(geom, b.geom))
                    Rows Removed by Filter: 2256
                    Buffers: shared hit=7560005
Planning:
  Buffers: shared hit=1
Planning Time: 0.213 ms
Execution Time: 14572.025 ms
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
<summary>Un día (poda a 1 partición) · 361.8 ms</summary>

```
GroupAggregate (actual rows=9596.00 loops=1)
  Group Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
  Buffers: shared hit=5835 read=6692, temp read=299 written=300
  ->  Gather Merge (actual rows=239694.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=5835 read=6692, temp read=299 written=300
        ->  Sort (actual rows=79898.00 loops=3)
              Sort Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
              Sort Method: external merge  Disk: 2392kB
              Buffers: shared hit=5835 read=6692, temp read=299 written=300
              Worker 0:  Sort Method: quicksort  Memory: 4051kB
              Worker 1:  Sort Method: quicksort  Memory: 4024kB
              ->  Parallel Seq Scan on telemetry_raw_p20260925 b (actual rows=79898.00 loops=3)
                    Filter: ((speed_kmh > '50'::double precision) AND (event_ts >= '2026-09-25 00:00:00+00'::timestamp with time zone) AND (event_ts < '2026-09-26 00:00:00+00'::timestamp with time zone))
                    Rows Removed by Filter: 160102
                    Buffers: shared hit=5759 read=6692
Planning:
  Buffers: shared hit=1
Planning Time: 0.247 ms
Execution Time: 339.293 ms
```
</details>

<details>
<summary>7 días (sin poda) · 2,740.7 ms</summary>

```
GroupAggregate (actual rows=9903.00 loops=1)
  Group Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
  Buffers: shared hit=13948 read=65041, temp read=5339 written=5360
  ->  Gather Merge (actual rows=1677397.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=13948 read=65041, temp read=5339 written=5360
        ->  Sort (actual rows=559132.33 loops=3)
              Sort Key: (floor((st_x(st_transform(b.geom, 9377)) / '250'::double precision))), (floor((st_y(st_transform(b.geom, 9377)) / '250'::double precision)))
              Sort Method: external merge  Disk: 14576kB
              Buffers: shared hit=13948 read=65041, temp read=5339 written=5360
              Worker 0:  Sort Method: external merge  Disk: 14248kB
              Worker 1:  Sort Method: external merge  Disk: 13888kB
              ->  Parallel Append (actual rows=559132.33 loops=3)
                    Buffers: shared hit=13872 read=65041
                    ->  Parallel Seq Scan on telemetry_raw_p20260927 b_6 (actual rows=239690.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480310
                          Buffers: shared hit=1178 read=10586
                    ->  Parallel Seq Scan on telemetry_raw_p20260926 b_5 (actual rows=239634.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480366
                          Buffers: shared hit=1180 read=10584
                    ->  Parallel Seq Scan on telemetry_raw_p20260922 b_1 (actual rows=239728.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480272
                          Buffers: shared hit=494 read=10583
                    ->  Parallel Seq Scan on telemetry_raw_p20260925 b_4 (actual rows=239694.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480306
                          Buffers: shared hit=5041 read=6036
                    ->  Parallel Seq Scan on telemetry_raw_p20260928 b_7 (actual rows=79830.00 loops=3)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 160170
                          Buffers: shared hit=4806 read=6271
                    ->  Parallel Seq Scan on telemetry_raw_p20260923 b_2 (actual rows=239611.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480389
                          Buffers: shared hit=494 read=10583
                    ->  Parallel Seq Scan on telemetry_raw_p20260924 b_3 (actual rows=239550.00 loops=1)
                          Filter: (speed_kmh > '50'::double precision)
                          Rows Removed by Filter: 480450
                          Buffers: shared hit=679 read=10398
Planning Time: 0.461 ms
Execution Time: 2020.373 ms
```
</details>

