"""Genera ``src/simon_geo/seed/data/bogota_open_data.geojson`` con geocercas reales.

Herramienta de desarrollo: se ejecuta UNA vez y el GeoJSON resultante se versiona,
así el proyecto funciona sin internet y es reproducible.

python scripts/fetch_bogota_open_data.py

Fuentes (todas de datos abiertos de Bogotá D.C.):

* Perímetro urbano (POT, Decreto 555 de 2021) — Secretaría Distrital de Planeación,
  CC BY 4.0. https://datosabiertos.bogota.gov.co/dataset/perimetro-urbano-bogota-d-c
* Colegios Bogotá D.C. (corte 12/2025) — Secretaría de Educación del Distrito,
  CC BY-SA 4.0. https://datosabiertos.bogota.gov.co/dataset/colegios-bogota-d-c
* Tramos y puntos críticos por siniestralidad de actores vulnerables en vías
  arteriales (2022–2025) — Oficina de Seguridad Vial, Secretaría Distrital de
  Movilidad, CC BY 4.0. https://sig.simur.gov.co/arcgis/rest/services/DatosAbiertos

Transformaciones (todas en metros, EPSG:9377):

* Perímetro: unión de los polígonos oficiales, simplificada a 10 m.
* Zona escolar: no hay una capa pública de "entornos escolares", así que se usa un
  radio de ``SCHOOL_RADIUS_M`` alrededor de cada sede (supuesto documentado). Solo
  se conservan el nombre, el código DANE y el sector; teléfonos y correos no.
* Tramo crítico: el polígono oficial tal cual.
* Punto crítico: un radio de ``CRITICAL_POINT_RADIUS_M`` alrededor de la intersección.

No se usan las "zonas críticas en vías locales": son áreas de cientos de hectáreas
definidas para la red local, y aplicarlas como geocerca agravaría la severidad
también en las arterias que las cruzan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import httpx
from pyproj import CRS, Transformer
from shapely import make_valid
from shapely import transform as shp_transform
from shapely.geometry import LinearRing, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import unary_union

from simon_geo.spatial.crs import to_metric, to_wgs84

OUT: Final = (
    Path(__file__).resolve().parents[1] / "src/simon_geo/seed/data/bogota_open_data.geojson"
)
SCHOOL_RADIUS_M: Final = 100.0  # ~ la cuadra frente a la sede
CRITICAL_POINT_RADIUS_M: Final = 100.0
PERIMETER_SIMPLIFY_M: Final = 10.0
BOGOTA_BBOX: Final = (-74.50, 3.70, -73.90, 4.90)  # lon/lat, incluye Sumapaz

DATOS: Final = "https://datosabiertos.bogota.gov.co/dataset"
PERIMETER_URL: Final = (
    f"{DATOS}/12a704ee-e5bb-4c5d-bad6-a5069d12f90a/resource/"
    "2b5ed242-8e16-4afb-9f85-13790170f874/download/perimetrourbano.json"
)
SCHOOLS_URL: Final = (
    f"{DATOS}/d451b52f-e30c-43b3-9066-3a7816638fea/resource/"
    "157d4822-d1d5-4d71-8c3e-1ac27e7421cb/download/colegios122025.geojson"
)
SIMUR: Final = "https://sig.simur.gov.co/arcgis/rest/services/DatosAbiertos"
CRITICAL_SEGMENTS_URL: Final = f"{SIMUR}/TramosCriticosVulnerables_ViaArterial_2225/FeatureServer/1"
CRITICAL_POINTS_URL: Final = f"{SIMUR}/PuntosCriticosVulnerables_ViaArterial_2225/FeatureServer/1"

SOURCES: Final = {
    "perimeter": "Perímetro urbano POT — Secretaría Distrital de Planeación (CC BY 4.0)",
    "schools": "Colegios Bogotá D.C. 12/2025 — Secretaría de Educación del Distrito (CC BY-SA 4.0)",
    "critical": "Tramos y puntos críticos de siniestralidad de vulnerables 2022–2025 — "
    "Oficina de Seguridad Vial, Secretaría Distrital de Movilidad (CC BY 4.0)",
}


def polygonal(geom: Any) -> Polygon | MultiPolygon:
    """Repara la geometría y se queda solo con su parte poligonal."""
    geom = make_valid(geom)
    if isinstance(geom, Polygon | MultiPolygon):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon | MultiPolygon)]
    return unary_union(parts)


def rounded(geom: Any) -> dict[str, Any]:
    """GeoJSON con 6 decimales (~10 cm). Redondear puede invalidar un polígono (vértices
    que colapsan), así que se valida después y se repara si hace falta."""
    out = json.loads(json.dumps(mapping(geom)), parse_float=lambda x: round(float(x), 6))
    if not shape(out).is_valid:
        out = json.loads(
            json.dumps(mapping(polygonal(shape(out)))),
            parse_float=lambda x: round(float(x), 6),
        )
    if not shape(out).is_valid:
        raise ValueError("geometría inválida tras redondear")
    return out


def feature(kind: str, code: str, name: str, geom: Any, **props: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"kind": kind, "code": code, "name": name, **props},
        "geometry": rounded(geom),
    }


def esri_polygon(rings: list[list[list[float]]]) -> Polygon | Any:
    """Anillos Esri (exteriores en sentido horario, huecos antihorario) → shapely."""
    outers = [r for r in rings if not LinearRing(r).is_ccw]
    holes = [r for r in rings if LinearRing(r).is_ccw]
    parts = []
    for outer in outers:
        shell = Polygon(outer)
        parts.append(
            Polygon(outer, [h for h in holes if shell.contains(Polygon(h).representative_point())])
        )
    return unary_union(parts)


def perimeter(client: httpx.Client) -> dict[str, Any]:
    data = client.get(PERIMETER_URL).raise_for_status().json()
    # Viene en Esri JSON y en el sistema local de Bogotá (CarMAGBOG): se reproyecta
    local = CRS.from_wkt(data["spatialReference"]["wkt"])
    to_lonlat = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
    geom = unary_union([esri_polygon(f["geometry"]["rings"]) for f in data["features"]])
    geom = shp_transform(geom, lambda xy: list(zip(*to_lonlat.transform(*xy.T), strict=True)))
    simplified = polygonal(
        to_wgs84(to_metric(geom).simplify(PERIMETER_SIMPLIFY_M, preserve_topology=True))
    )
    print(f"perímetro: {to_metric(simplified).area / 1e6:.1f} km²")
    return feature(
        "urban", "BOG-URB", "Perímetro urbano de Bogotá (POT)", simplified, source="perimeter"
    )


def schools(client: httpx.Client) -> list[dict[str, Any]]:
    data = client.get(SCHOOLS_URL).raise_for_status().json()
    from_mercator = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for f in data["features"]:
        p, g = f["properties"], f["geometry"]
        if not g or g["type"] != "Point":
            continue
        lon, lat = from_mercator.transform(*g["coordinates"])
        # algunas sedes vienen sin ubicación (coordenada centinela ±1.8e308)
        if not (
            BOGOTA_BBOX[0] <= lon <= BOGOTA_BBOX[2] and BOGOTA_BBOX[1] <= lat <= BOGOTA_BBOX[3]
        ):
            skipped += 1
            continue
        code = f"ESC-{p['DANE12_SED']}-{p.get('ORDEN_DE_S') or 'A'}"
        if code in seen:  # sedes repetidas en la fuente: se deja la primera
            continue
        seen.add(code)
        zone = to_wgs84(to_metric(Point(lon, lat)).buffer(SCHOOL_RADIUS_M, quad_segs=4))
        name = (p.get("NOMBRE_SED") or p.get("NOMBRE_EST") or "Colegio").strip().title()
        out.append(
            feature(
                "school",
                code,
                f"Zona escolar · {name}",
                zone,
                sector="oficial" if p.get("SECTOR") == 1 else "privado",
                source="schools",
            )
        )
    print(f"colegios: {len(out)} (descartados sin ubicación válida: {skipped})")
    return out


def arcgis_features(client: httpx.Client, layer_url: str) -> list[dict[str, Any]]:
    params = {"where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson"}
    return client.get(f"{layer_url}/query", params=params).raise_for_status().json()["features"]


def critical(client: httpx.Client) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in arcgis_features(client, CRITICAL_SEGMENTS_URL):
        p = f["properties"]
        out.append(
            feature(
                "high_risk",
                f"SDM-TC-{p['ID_TRAMO_TC_ART']}",
                f"Tramo crítico · {p['DIRECCION_TC_ART']}",
                shape(f["geometry"]),
                fatalities=int(p["FALLECIDOS_TC_ART"] or 0),
                injured_vulnerable=int(p["LESIONADO_VU_TC_ART"] or 0),
                source="critical",
            )
        )
    for f in arcgis_features(client, CRITICAL_POINTS_URL):
        p = f["properties"]
        zone = to_wgs84(to_metric(shape(f["geometry"])).buffer(CRITICAL_POINT_RADIUS_M, 8))
        out.append(
            feature(
                "high_risk",
                f"SDM-PC-{p['ID_PUNTO_PC_ART']}",
                f"Punto crítico · {p['DIRECCION_PC_ART']}",
                zone,
                fatalities=int(p["FALLECIDOS_PC_ART"] or 0),
                injured_vulnerable=int(p["LESIONADO_VU_PC_ART"] or 0),
                source="critical",
            )
        )
    print(f"tramos y puntos críticos: {len(out)}")
    return out


def main() -> None:
    with httpx.Client(timeout=90, headers={"User-Agent": "simon-geo-demo/0.1"}) as client:
        features = [perimeter(client), *critical(client), *schools(client)]
    collection = {
        "type": "FeatureCollection",
        "sources": SOURCES,
        "assumptions": {
            "school_radius_m": SCHOOL_RADIUS_M,
            "critical_point_radius_m": CRITICAL_POINT_RADIUS_M,
        },
        "features": features,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(collection, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Escrito {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
