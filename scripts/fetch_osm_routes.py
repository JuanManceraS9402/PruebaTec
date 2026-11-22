"""Genera ``src/gps_flota/seed/data/bogota_osm.geojson`` (rutas y corredores reales).

Herramienta de desarrollo: se ejecuta UNA vez y el GeoJSON resultante se versiona,
así el proyecto funciona sin internet y es reproducible.

python scripts/fetch_osm_routes.py                 # todo
python scripts/fetch_osm_routes.py --only R04,R05  # solo esas rutas; el resto se conserva

* Las rutas se calculan con OSRM (servidor público de demostración) sobre la red
  vial de OpenStreetMap, pasando por puntos de paso verificados sobre las
  arterias que debe recorrer cada ruta.
* Los corredores de geocerca se toman de los tramos de la ruta cuyo nombre de vía
  coincide (p. ej. "Avenida Carrera 45" = Autopista Norte).
* Las geometrías se simplifican a 5 m (en EPSG:9377) para no versionar miles de
  vértices que no aportan a la demo.

Datos © colaboradores de OpenStreetMap, licencia ODbL.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Final

import httpx
from shapely.geometry import LineString, MultiLineString, mapping
from shapely.ops import linemerge

from gps_flota.spatial.crs import to_metric, to_wgs84

OSRM: Final = "https://router.project-osrm.org/route/v1/driving/"
OUT: Final = Path(__file__).resolve().parents[1] / "src/gps_flota/seed/data/bogota_osm.geojson"
SIMPLIFY_M: Final = 5.0

# Puntos de paso (lon, lat), verificados con Nominatim sobre la vía correspondiente
DEPOT: Final = (-74.1500, 4.6680)  # patio logístico, Fontibón
CALLE26_FONTIBON: Final = (-74.1148, 4.6708)  # Av. Calle 26 (El Dorado)
HEROES: Final = (-74.0607, 4.6674)  # Calle 80 con Autopista Norte
PORTAL_NORTE: Final = (-74.0460, 4.7546)  # Autopista Norte (Av. Carrera 45)
PORTAL_SUR: Final = (-74.1693, 4.5972)  # Autopista Sur (Av. Calle 57R Sur)
SOACHA: Final = (-74.2314, 4.5604)  # Autopista Sur, Soacha
PARKWAY: Final = (-74.0750, 4.6295)  # Park Way, Teusaquillo (zona escolar)
CALLE26_CENTRO: Final = (-74.0704, 4.6120)  # Av. Calle 26, Centro Internacional
# Rutas por tramos críticos de siniestralidad (SDM 2022-2025, ver fetch_bogota_open_data)
AMERICAS_W: Final = (-74.1461, 4.6311)  # Av. Américas entre Cra 80 y Av. Boyacá
AMERICAS_E: Final = (-74.1198, 4.6285)  # Av. Américas entre Cra 50 y Cra 71A
NQS_SUR: Final = (-74.0970, 4.6086)  # Av. NQS entre Calle 22A y Calle 8 sur
CALLE19: Final = (-74.0775, 4.6093)  # Calle 19 entre Cra 9 y Cra 20
CALI_CENTRO: Final = (-74.1552, 4.6418)  # Av. Ciudad de Cali entre Diag. 3 y Calle 6D
CALI_SUR: Final = (-74.1689, 4.6299)  # Av. Ciudad de Cali entre Calle 40B y 45 bis sur
AV_BOSA: Final = (-74.1899, 4.6238)  # Av. Bosa entre Cra 87 y Cra 88C
CALLE72_BOYACA: Final = (-74.1009, 4.6917)  # Calle 72 entre Av. Boyacá y Av. Ciudad de Cali
BOYACA_CALLE80: Final = (-74.0891, 4.6954)  # Av. Boyacá x Calle 80
CALI_CRA91: Final = (-74.0928, 4.7172)  # Av. Ciudad de Cali x Cra 91
SUBA_CALI: Final = (-74.0963, 4.7466)  # Av. Suba x Av. Ciudad de Cali

ROUTES: Final = {
    "R01": (
        "Fontibón → Calle 26 → Calle 80 → Autopista Norte",
        [DEPOT, CALLE26_FONTIBON, HEROES, PORTAL_NORTE],
    ),
    "R02": ("Fontibón → Av. Boyacá → Autopista Sur → Soacha", [DEPOT, PORTAL_SUR, SOACHA]),
    "R03": (
        "Fontibón → Calle 26 → Teusaquillo → Centro",
        [DEPOT, CALLE26_FONTIBON, PARKWAY, CALLE26_CENTRO],
    ),
    "R04": (
        "Fontibón → Av. Américas → NQS → Centro",
        [DEPOT, AMERICAS_W, AMERICAS_E, NQS_SUR, CALLE19],
    ),
    "R05": (
        "Fontibón → Av. Ciudad de Cali → Kennedy → Bosa",
        [DEPOT, CALI_CENTRO, CALI_SUR, AV_BOSA],
    ),
    "R06": (
        "Fontibón → Av. Boyacá → Calle 80 → Suba",
        [DEPOT, CALLE72_BOYACA, BOYACA_CALLE80, CALI_CRA91, SUBA_CALI],
    ),
}

# Corredor → (ruta, nombres de vía en OSM que lo forman)
CORRIDORS: Final = {
    "AUTONORTE": ("R01", {"Avenida Carrera 45"}),
    "AUTOSUR": ("R02", {"Avenida Calle 57R Sur", "Autopista Sur"}),
}

LANDMARKS: Final = {"DEPOT": DEPOT, "HEROES": HEROES, "PARKWAY": PARKWAY}


def simplify(line: LineString) -> LineString:
    return to_wgs84(to_metric(line).simplify(SIMPLIFY_M, preserve_topology=True))


def rounded(geom: Any) -> dict[str, Any]:
    """GeoJSON con 6 decimales (~10 cm): suficiente y legible en un diff."""
    return json.loads(json.dumps(mapping(geom)), parse_float=lambda x: round(float(x), 6))


def fetch_route(client: httpx.Client, waypoints: list[tuple[float, float]]) -> dict[str, Any]:
    coords = ";".join(f"{lon},{lat}" for lon, lat in waypoints)
    resp = client.get(
        OSRM + coords, params={"overview": "full", "geometries": "geojson", "steps": "true"}
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM respondió {data.get('code')}: {data.get('message')}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="Rutas a (re)calcular, p. ej. R04,R05; conserva el resto")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else set(ROUTES)

    features: list[dict[str, Any]] = []
    if args.only and OUT.exists():  # conservar lo ya versionado que no se recalcula
        kept = json.loads(OUT.read_text(encoding="utf-8"))["features"]
        features = [
            f
            for f in kept
            if not (f["properties"]["kind"] == "route" and f["properties"]["code"] in only)
            and not (f["properties"]["kind"] == "corridor" and f["properties"]["route"] in only)
            and f["properties"]["kind"] != "landmark"
        ]
    routes: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=30, headers={"User-Agent": "gps-flota-demo/0.1"}) as client:
        for code, (name, waypoints) in ROUTES.items():
            if code not in only:
                continue
            routes[code] = fetch_route(client, waypoints)
            geometry = LineString(routes[code]["routes"][0]["geometry"]["coordinates"])
            features.append(
                {
                    "type": "Feature",
                    "properties": {"kind": "route", "code": code, "name": name},
                    "geometry": rounded(simplify(geometry)),
                }
            )
            print(f"{code}: {routes[code]['routes'][0]['distance'] / 1000:.1f} km")

    for code, (route_code, road_names) in CORRIDORS.items():
        if route_code not in only:
            continue
        steps = [
            LineString(step["geometry"]["coordinates"])
            for leg in routes[route_code]["routes"][0]["legs"]
            for step in leg["steps"]
            if step["name"] in road_names and len(step["geometry"]["coordinates"]) >= 2
        ]
        merged = linemerge(MultiLineString(steps))
        if merged.geom_type != "LineString":  # tramos no contiguos: el más largo
            merged = max(merged.geoms, key=lambda g: to_metric(g).length)
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": "corridor", "code": code, "route": route_code},
                "geometry": rounded(simplify(merged)),
            }
        )
        print(f"corredor {code}: {to_metric(merged).length / 1000:.1f} km")

    for name, (lon, lat) in LANDMARKS.items():
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": "landmark", "code": name},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )

    collection = {
        "type": "FeatureCollection",
        "attribution": "Datos © colaboradores de OpenStreetMap (ODbL). Rutas calculadas con OSRM.",
        "features": features,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(collection, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Escrito {OUT}")


if __name__ == "__main__":
    main()
