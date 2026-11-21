"""Transformaciones del tablero y arranque de la app sin base de datos."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from simon_geo.dashboard.views import (
    BOGOTA_VIEW,
    SEVERITY_COLORS,
    feature_collection,
    fleet_summary,
    line_path,
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
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DB_PORT", "1")  # puerto cerrado: falla rápido y de forma determinista
    from simon_geo.config import get_settings

    get_settings.cache_clear()
    try:
        at = AppTest.from_file(str(APP), default_timeout=30).run()
    finally:
        get_settings.cache_clear()
    assert not at.exception
    assert any("PostGIS" in e.value for e in at.error)
