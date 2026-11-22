"""Estilo híbrido: la ortofoto queda entre el terreno y las vías/nombres de CARTO."""

from __future__ import annotations

from gps_flota.dashboard.basemap import ORTHO_ATTRIBUTION, hybrid_style

BASE = {
    "version": 8,
    "sources": {"carto": {"type": "vector", "url": "https://example/tiles.json"}},
    "layers": [
        {"id": "background", "type": "background"},
        {"id": "landuse", "type": "fill", "source": "carto"},
        {"id": "water", "type": "fill", "source": "carto"},
        {"id": "road_minor_fill", "type": "line", "source": "carto", "paint": {}},
        {"id": "building", "type": "fill", "source": "carto"},
        {"id": "place_town", "type": "symbol", "source": "carto"},
    ],
}


def ids(style: dict) -> list[str]:
    return [layer["id"] for layer in style["layers"]]


def test_ortho_goes_over_terrain_and_under_roads_and_labels() -> None:
    style = hybrid_style(BASE)
    assert ids(style) == [
        "background",
        "landuse",
        "water",
        "ortofoto",
        "road_minor_fill",
        "place_town",
    ]


def test_buildings_removed_and_roads_translucent() -> None:
    style = hybrid_style(BASE)
    road = next(layer for layer in style["layers"] if layer["id"] == "road_minor_fill")
    assert road["paint"]["line-opacity"] < 1
    assert "building" not in ids(style)


def test_ortho_source_is_attributed_and_transparent_at_edges() -> None:
    source = hybrid_style(BASE)["sources"]["ortofoto"]
    assert source["attribution"] == ORTHO_ATTRIBUTION
    assert "format=jpgpng" in source["tiles"][0]
    assert "carto" in hybrid_style(BASE)["sources"]


def test_base_style_not_mutated() -> None:
    before = repr(BASE)
    hybrid_style(BASE)
    assert repr(BASE) == before
