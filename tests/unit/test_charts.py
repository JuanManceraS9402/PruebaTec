"""Gráficos del tablero: especificaciones válidas y reglas de color y de datos."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from simon_geo.dashboard import charts
from simon_geo.dashboard.views import SEVERITY_COLORS


@pytest.fixture
def kpis() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "label": ["VHC-****-BOG · VK-2", "VHC-****-ABC · VK-1"],
            "vehicle_key": ["VK-2", "VK-1"],
            "driver_score": [3.5, 100.0],
            "speeding_time_pct": [92.0, 0.0],
            "distance_km": [23.6, 27.4],
            "harsh_events": [4, 0],
        }
    )


@pytest.fixture
def alerts() -> pd.DataFrame:
    ts = pd.to_datetime(["2026-11-20 06:01", "2026-11-20 06:02", "2026-11-20 06:31"]).tz_localize(
        "America/Bogota"
    )
    return pd.DataFrame(
        {
            "event_ts": ts,
            "severity": ["LOW", "CRITICAL", "LOW"],
            "geofence_name": ["Zona escolar", None, "Zona escolar"],
        }
    )


@pytest.fixture
def profile() -> pd.DataFrame:
    n = 4
    return pd.DataFrame(
        {
            "event_ts": pd.date_range("2026-11-20 06:00", periods=n, freq="min", tz="UTC"),
            "speed_kmh": [40.0, 70.0, 75.0, 45.0],
            "applied_speed_limit_kmh": [50.0] * n,
            "speed_excess_kmh": [0.0, 20.0, 25.0, 0.0],
            "is_speeding": [False, True, True, False],
            "fuel_level_pct": [10.0] * n,
            "est_range_km": [30.0, 20.0, 10.0, 5.0],
            "remaining_route_km": [20.0, 15.0, 10.0, 5.0],
            "is_low_fuel_risk": [False, False, True, True],
        }
    )


def datasets(chart) -> list[list[dict]]:
    return list(chart.to_dict()["datasets"].values())


def test_hex_color() -> None:
    assert charts.hex_color([0, 114, 178, 255]) == "#0072b2"


def test_ranking_keeps_entity_color_and_exposes_selection(kpis) -> None:
    chart = charts.score_ranking(kpis, {"VK-1": "#0072b2", "VK-2": "#d55e00"})
    spec = chart.to_dict()
    rows = datasets(chart)[0]
    assert {r["vehicle_key"]: r["color"] for r in rows} == {"VK-1": "#0072b2", "VK-2": "#d55e00"}
    params = [p["name"] for p in spec["params"]]
    assert charts.SELECTION in params


def test_zone_chart_counts_and_uses_severity_palette(alerts) -> None:
    chart = charts.infractions_by_zone(alerts)
    rows = datasets(chart)[0]
    assert sum(r["size"] for r in rows) == len(alerts)
    assert {r["zone"] for r in rows} == {"Zona escolar", "Fuera de geocerca"}
    scale = chart.to_dict()["encoding"]["color"]["scale"]
    assert scale["range"] == [charts.hex_color(c) for c in SEVERITY_COLORS.values()]


def test_zone_type_chart_has_one_bar_per_type() -> None:
    alerts = pd.DataFrame(
        {
            "severity": ["LOW", "HIGH", "LOW", "CRITICAL"],
            "zone_type": ["SCHOOL", "SCHOOL", "HIGH_RISK", None],
            "geofence_name": ["Colegio A", "Colegio B", "Tramo", None],
        }
    )
    rows = datasets(charts.infractions_by_zone_type(alerts))[0]
    assert {r["zone"] for r in rows} == {
        "Zona escolar",
        "Alta siniestralidad",
        "Fuera de geocerca",
    }
    assert sum(r["size"] for r in rows) == len(alerts)


def test_top_zones_folds_the_rest_without_losing_alerts() -> None:
    names = ["A", "A", "A", "B", "B", "C", "D"]
    alerts = pd.DataFrame({"severity": ["LOW"] * len(names), "geofence_name": names})
    chart = charts.infractions_by_zone(alerts, top=2)
    rows = datasets(chart)[0]
    assert {r["zone"]: r["size"] for r in rows} == {"A": 3, "B": 2, "Otras 2 zonas": 2}
    assert chart.to_dict()["encoding"]["y"]["sort"] == ["A", "B", "Otras 2 zonas"]


@pytest.mark.parametrize(
    ("span", "minutes"),
    [(pd.Timedelta(minutes=40), 5), (pd.Timedelta(hours=5), 15), (pd.Timedelta(hours=20), 60)],
)
def test_time_bucket_adapts_to_span(span, minutes) -> None:
    assert charts.time_bucket_minutes(span) == minutes


def test_over_time_floors_to_bucket_in_local_time(alerts) -> None:
    rows = datasets(charts.infractions_over_time(alerts))[0]
    assert sorted({r["t"] for r in rows}) == ["2026-11-20T06:00:00", "2026-11-20T06:30:00"]


def test_profile_spec_is_valid_and_shades_only_flagged_gaps(profile) -> None:
    chart = charts.vehicle_profile(profile, "#0072b2", 1.15)
    spec = chart.to_dict()
    json.dumps(spec)  # serializable para el frontend
    names = [p["name"] for p in spec["params"]]
    assert len(names) == len(set(names))  # sin señales duplicadas en Vega
    gaps = [pd.DataFrame(d) for d in datasets(chart) if d and "gap_to" in d[0]]

    def shades(frame: pd.DataFrame, measure: str, reference: str, flag: str) -> bool:
        on = frame[flag]
        return bool(
            (frame.loc[on, "gap_to"] == frame.loc[on, reference]).all()
            and (frame.loc[~on, "gap_to"] == frame.loc[~on, measure]).all()
        )

    assert any(shades(f, "speed", "limit", "speeding") for f in gaps)
    assert any(shades(f, "range_km", "need_km", "fuel_risk") for f in gaps)


def test_profile_required_distance_applies_safety_factor(profile) -> None:
    chart = charts.vehicle_profile(profile, "#0072b2", 1.15)
    frame = next(pd.DataFrame(d) for d in datasets(chart) if d and "need_km" in d[0])
    assert frame["need_km"].tolist() == pytest.approx([23.0, 17.25, 11.5, 5.75])
