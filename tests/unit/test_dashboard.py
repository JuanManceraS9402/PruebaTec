"""Transformaciones del tablero y arranque de la app sin base de datos."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gps_flota.dashboard.views import (
    BOGOTA_VIEW,
    HEATMAP_COLORS,
    SEVERITY_COLORS,
    build_legend,
    compact_vehicle_labels,
    feature_collection,
    fleet_summary,
    geofences_on_paths,
    legend_html,
    line_path,
    reconcile_selection,
    vehicle_label,
    view_for,
    with_colors,
)

APP = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"


def kpis(*rows: tuple[str, str, float, float | None]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["vehicle_id", "vehicle_key", "distance_km", "driver_score"])


def alerts(*types: str) -> pd.DataFrame:
    return pd.DataFrame({"alert_type": list(types), "lon": -74.1, "lat": 4.6})


class TestLinePath:
    def test_linestring(self) -> None:
        geojson = json.dumps({"type": "LineString", "coordinates": [[-74.1, 4.6], [-74.2, 4.7]]})
        assert line_path(geojson) == [[-74.1, 4.6], [-74.2, 4.7]]

    def test_empty_track(self) -> None:
        assert line_path(None) == []

    def test_rejects_other_geometries(self) -> None:
        with pytest.raises(ValueError):
            line_path(json.dumps({"type": "Point", "coordinates": [0, 0]}))


class TestFeatureCollection:
    def test_builds_features_and_skips_missing_geometry(self) -> None:
        square = json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})
        rows = [
            {"g": square, "name": "A", "limit": Decimal("30"), "n": np.int64(4)},
            {"g": None, "name": "B", "limit": None, "n": 1},
        ]
        fc = feature_collection(rows, "g", ("name", "limit", "n"))
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) == 1
        props = fc["features"][0]["properties"]
        assert props == {"name": "A", "limit": 30.0, "n": 4}
        json.dumps(fc)  # serializable para pydeck

    def test_nan_becomes_null(self) -> None:
        point = json.dumps({"type": "Point", "coordinates": [0, 0]})
        fc = feature_collection([{"g": point, "x": float("nan")}], "g", ("x",))
        assert fc["features"][0]["properties"]["x"] is None


class TestFleetSummary:
    def test_summary(self) -> None:
        s = fleet_summary(
            kpis(("VHC-****-ABC", "VK-1", 10.0, 95.0), ("VHC-****-BOG", "VK-2", 5.5, 40.0)),
            alerts("SPEEDING", "SPEEDING", "LOW_FUEL_RANGE"),
        )
        assert s.vehicles == 2
        assert s.distance_km == pytest.approx(15.5)
        assert s.avg_driver_score == pytest.approx(67.5)
        assert s.worst_vehicle == "VHC-****-BOG · VK-2"
        assert (s.speeding_alerts, s.fuel_alerts) == (2, 1)

    def test_empty_day(self) -> None:
        s = fleet_summary(kpis(), alerts())
        assert (s.vehicles, s.distance_km, s.avg_driver_score, s.worst_vehicle) == (
            0,
            0,
            None,
            None,
        )

    def test_scores_missing(self) -> None:
        s = fleet_summary(kpis(("VHC-****-ABC", "VK-1", 1.0, None)), alerts())
        assert s.avg_driver_score is None and s.worst_vehicle is None


def test_vehicle_label_disambiguates_masked_ids() -> None:
    assert vehicle_label("VHC-****-ABC", "VK-1") != vehicle_label("VHC-****-ABC", "VK-2")
    assert vehicle_label("VHC-8472-ABC", None) == "VHC-8472-ABC"


def test_with_colors_uses_palette_and_fallback() -> None:
    out = with_colors(pd.DataFrame({"severity": ["HIGH", "???"]}), "severity", SEVERITY_COLORS)
    assert out["color"][0] == SEVERITY_COLORS["HIGH"]
    assert len(out["color"][1]) == 4


class TestViewFor:
    def test_defaults_to_bogota(self) -> None:
        assert view_for([], alerts()) == BOGOTA_VIEW

    def test_centers_on_data(self) -> None:
        v = view_for([[[-74.0, 4.0], [-74.2, 4.2]]], pd.DataFrame({"lon": [-74.1], "lat": [4.1]}))
        assert v["longitude"] == pytest.approx(-74.1)
        assert v["latitude"] == pytest.approx(4.1)


def test_app_starts_without_crashing(monkeypatch) -> None:
    """La app arranca; sin base de datos muestra un error guiado en vez de una traza."""
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_resource.clear()  # que no reutilice una conexión abierta por otra prueba
    st.cache_data.clear()
    monkeypatch.setenv("DB_PORT", "1")  # puerto cerrado: falla rápido y de forma determinista
    from gps_flota.config import get_settings

    get_settings.cache_clear()
    try:
        at = AppTest.from_file(str(APP), default_timeout=30).run()
    finally:
        get_settings.cache_clear()
    assert not at.exception
    assert any("PostGIS" in e.value for e in at.error)


class TestLegend:
    ALL_ON = {"geofences": True, "tracks": True, "heatmap": True, "alerts": True, "hotspots": True}
    ZONES = [("URBAN", 50), ("SCHOOL", 30), ("HIGHWAY", 50), ("HIGH_RISK", 50), ("DEPOT", 20)]

    def legend(self, enabled=None, **kw):
        defaults = {
            "zones": self.ZONES,
            "tracks": [("VHC-****-ABC · VK-1", [0, 114, 178])],
            "severities": ["HIGH", "LOW"],
        }
        return build_legend(enabled or self.ALL_ON, **{**defaults, **kw})

    def test_only_enabled_layers_appear(self) -> None:
        titles = [s.title for s in self.legend({"alerts": True, "geofences": True})]
        assert titles == ["Puntos de infracción", "Geocercas"]

    def test_sections_follow_map_stacking_order(self) -> None:
        titles = [s.title for s in self.legend()]
        assert titles == [
            "Puntos de infracción",
            "Rutas del día",
            "Mapa de calor",
            "Zonas calientes (histórico)",
            "Geocercas",
        ]

    def test_severities_only_those_present_in_order(self) -> None:
        alerts = self.legend({"alerts": True})[0]
        assert [i.label.split(" ")[0] for i in alerts.items] == ["Baja", "Alta"]
        assert alerts.items[1].colors[0] == list(SEVERITY_COLORS["HIGH"])

    def test_geofences_show_limits_by_priority(self) -> None:
        zones = self.legend({"geofences": True})[0]
        assert [i.label for i in zones.items] == [
            "Zona escolar · 30 km/h",
            "Alta siniestralidad · 50 km/h",
            "Patio logístico · 20 km/h",
            "Corredor vial · 50 km/h",
            "Perímetro urbano · 50 km/h",
        ]

    def test_no_alerts_hides_alert_and_heatmap_sections(self) -> None:
        titles = [s.title for s in self.legend(severities=[])]
        assert "Puntos de infracción" not in titles and "Mapa de calor" not in titles

    def test_heatmap_ramp_matches_layer_colors(self) -> None:
        heat = next(s for s in self.legend() if s.title == "Mapa de calor")
        assert heat.items[0].colors == HEATMAP_COLORS

    def test_html_escapes_and_renders(self) -> None:
        out = legend_html(self.legend(tracks=[("<script>x</script>", [1, 2, 3])]))
        assert "<script>" not in out and "&lt;script&gt;" in out
        assert "linear-gradient" in out  # rampa del mapa de calor
        assert legend_html([]).count("No hay capas") == 1


class TestVehicleFilter:
    def test_labels_are_short(self) -> None:
        labels = compact_vehicle_labels([("VHC-8472-ABC", "VK-1111"), ("VHC-****-QWE", "VK-2222")])
        assert labels == {"VK-1111": "8472-ABC", "VK-2222": "****-QWE"}

    def test_repeated_masked_ids_stay_distinct_without_revealing_digits(self) -> None:
        labels = compact_vehicle_labels([("VHC-****-ABC", "VK-aaaa"), ("VHC-****-ABC", "VK-bbbb")])
        assert labels == {"VK-aaaa": "****-ABC · aaaa", "VK-bbbb": "****-ABC · bbbb"}

    def test_removed_vehicle_leaves_selection(self) -> None:
        assert reconcile_selection(["a", "b"], ["a", "gone"], ["a", "b", "gone"]) == ["a"]

    def test_new_vehicle_starts_on_but_hidden_ones_stay_off(self) -> None:
        assert reconcile_selection(["a", "b", "new"], ["a"], ["a", "b"]) == ["a", "new"]

    def test_none_selected_stays_empty(self) -> None:
        assert reconcile_selection(["a", "b"], [], ["a", "b"]) == []


class TestGeofencesOnPaths:
    @staticmethod
    def square(lon: float, lat: float, half: float = 0.001) -> str:
        ring = [
            [lon - half, lat - half],
            [lon + half, lat - half],
            [lon + half, lat + half],
            [lon - half, lat + half],
            [lon - half, lat - half],
        ]
        return json.dumps({"type": "Polygon", "coordinates": [ring]})

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "code": ["URB", "ESC-ON", "ESC-OFF", "RISK-OFF"],
                "zone_type": ["URBAN", "SCHOOL", "SCHOOL", "HIGH_RISK"],
                "geojson": [
                    self.square(-74.1, 4.65, 0.5),
                    self.square(-74.10, 4.65),
                    self.square(-74.00, 4.70),
                    self.square(-74.00, 4.70),
                ],
            }
        )

    def test_keeps_schools_on_route_and_every_other_zone(self) -> None:
        out = geofences_on_paths(self.frame(), [[[-74.11, 4.65], [-74.09, 4.65]]])
        assert list(out["code"]) == ["URB", "ESC-ON", "RISK-OFF"]

    def test_no_routes_hides_all_schools(self) -> None:
        out = geofences_on_paths(self.frame(), [])
        assert "SCHOOL" not in set(out["zone_type"])


def test_orthophoto_style_is_served_and_attributed() -> None:
    """La ortofoto es un estilo MapLibre servido por la app (enableStaticServing)."""
    root = APP.parents[1]
    style = json.loads((APP.parent / "static" / "ortofoto_bogota_2025.json").read_text("utf-8"))
    assert style["version"] == 8
    source = style["sources"]["ortofoto"]
    assert "{bbox-epsg-3857}" in source["tiles"][0] and "exportImage" in source["tiles"][0]
    assert "IDECA" in source["attribution"]
    assert "enableStaticServing = true" in (root / ".streamlit" / "config.toml").read_text("utf-8")
