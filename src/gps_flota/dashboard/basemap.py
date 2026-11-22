"""Mapa base híbrido: ortofoto de Bogotá bajo las vías y los nombres de CARTO.

La ortofoto urbana 2025 (Catastro / IDECA) solo cubre el área urbana, y sola no
tiene nombres de vías. Aquí se toma el estilo vectorial Positron de CARTO (el mismo
del modo "Mapa", sin clave) y se inserta la ortofoto encima del terreno y debajo de
las vías y las etiquetas. Fuera de la cobertura, las teselas de la ortofoto son
transparentes (``format=jpgpng``) y se ve el mapa de CARTO.

El estilo de CARTO lleva URLs versionadas que cambian con el tiempo, así que no se
versiona una copia: el tablero lo descarga al arrancar y, si no hay internet, usa
solo la ortofoto (``dashboard/static/ortofoto_bogota_2025.json``).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Final

POSITRON_STYLE_URL: Final = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
ORTHO_SERVICE: Final = (
    "https://serviciosgis.catastrobogota.gov.co/image/rest/services/imagenesfunciones/"
    "orthourbana2025funcion/ImageServer"
)
# jpgpng: JPG donde la tesela está completa, PNG transparente en el borde de cobertura
ORTHO_TILES: Final = (
    f"{ORTHO_SERVICE}/exportImage?bbox={{bbox-epsg-3857}}&bboxSR=3857&imageSR=3857"
    "&size=256,256&format=jpgpng&bandIds=0,1,2&f=image"
)
ORTHO_ATTRIBUTION: Final = (
    "Ortofoto urbana 2025 © Secretaría Distrital de Planeación · IDECA – UAECD"
)
ORTHO_BOUNDS: Final = [-74.225, 4.486, -74.019, 4.838]
# Capas del estilo base que van por encima de la ortofoto (vías, nombres, límites)
_OVER_ORTHO_PREFIXES: Final = ("tunnel", "road", "bridge", "rail", "aeroway")
_ROAD_OPACITY: Final = 0.55


def ortho_source() -> dict[str, Any]:
    return {
        "type": "raster",
        "tiles": [ORTHO_TILES],
        "tileSize": 256,
        "minzoom": 10,
        "maxzoom": 21,
        "bounds": ORTHO_BOUNDS,
        "attribution": ORTHO_ATTRIBUTION,
    }


def hybrid_style(base: Mapping[str, Any]) -> dict[str, Any]:
    """Inserta la ortofoto en un estilo MapLibre justo antes de la primera capa de vías.

    * Se quitan los edificios 3D/planos del estilo base: taparían la foto.
    * Las vías se dejan semitransparentes para que se vea el pavimento real.
    * Etiquetas y demás capas se conservan en su orden.
    """
    style = copy.deepcopy(dict(base))
    layers = [layer for layer in style["layers"] if not layer["id"].startswith("building")]
    insert_at = next(
        (i for i, layer in enumerate(layers) if layer["id"].startswith(_OVER_ORTHO_PREFIXES)),
        len(layers),
    )
    for layer in layers[insert_at:]:
        if layer["type"] == "line" and layer["id"].startswith(_OVER_ORTHO_PREFIXES):
            layer.setdefault("paint", {})["line-opacity"] = _ROAD_OPACITY
    layers.insert(insert_at, {"id": "ortofoto", "type": "raster", "source": "ortofoto"})
    style["layers"] = layers
    style["sources"] = {**style["sources"], "ortofoto": ortho_source()}
    style["name"] = "Ortofoto Bogotá 2025 + CARTO Positron"
    return style
