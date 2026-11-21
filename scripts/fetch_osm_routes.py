"""Genera ``src/simon_geo/seed/data/bogota_osm.geojson`` (rutas y corredores reales).

Herramienta de desarrollo: se ejecuta UNA vez y el GeoJSON resultante se versiona,
así el proyecto funciona sin internet y es reproducible.

python scripts/fetch_osm_routes.py

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

import json
from pathlib import Path
from typing import Any, Final

import httpx
from shapely.geometry import LineString, MultiLineString, mapping
from shapely.ops import linemerge

from simon_geo.spatial.crs import to_metric, to_wgs84

OSRM: Final = "https://router.project-osrm.org/route/v1/driving/"
OUT: Final = Path(__file__).resolve().parents[1] / "src/simon_geo/seed/data/bogota_osm.geojson"
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
    features: list[dict[str, Any]] = []
    routes: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=30, headers={"User-Agent": "simon-geo-demo/0.1"}) as client:
        for code, (name, waypoints) in ROUTES.items():
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
