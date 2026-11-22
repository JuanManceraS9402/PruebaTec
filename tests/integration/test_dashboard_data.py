"""Consultas del tablero contra PostGIS: el enmascaramiento lo decide la base."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gps_flota.config import DbRole
from gps_flota.dashboard import data

pytestmark = pytest.mark.integration

MASKED = re.compile(r"^[A-Z]{3}-\*{4}-[A-Z]{3}$")
REAL = re.compile(r"^[A-Z]{3}-[0-9]{4}-[A-Z]{3}$")
APP = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"


@pytest.mark.parametrize(("role", "pattern"), [(DbRole.ANALYST, MASKED), (DbRole.ADMIN, REAL)])
def test_queries_run_and_mask_by_role(conn, seeded, role, pattern) -> None:
    c = conn(role)
    user, is_admin = data.whoami(c)
    assert is_admin is (role is DbRole.ADMIN)

    geofences = data.geofences(c)
    assert set(seeded) <= set(geofences["code"])
    hotspots = data.infraction_hotspots(c)
    assert list(hotspots["hotspot_rank"]) == list(range(1, len(hotspots) + 1))
    assert hotspots["infractions"].is_monotonic_decreasing

    for day in data.available_days(c)[:3]:
        kpis = data.daily_kpis(c, day)
        alerts = data.infractions(c, day)
        ids = list(kpis["vehicle_id"]) + list(alerts["vehicle_id"])
        assert all(pattern.match(v) for v in ids), ids[:5]
        assert kpis["vehicle_key"].str.startswith("VK-").all()
        for key in kpis["vehicle_key"][:1]:
            profile = data.vehicle_profile(c, day, key)
            assert profile["event_ts"].is_monotonic_increasing


def test_app_renders_with_database(seeded) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert not at.error
    # pydeck convierte cualquier texto en expresión: "pixels" sin envolver queda como
    # "@@=pixels" (indefinido) y las rutas se dibujan gigantes en el mapa
    for deck in at.get("deck_gl_json_chart"):
        for layer in json.loads(deck.proto.json)["layers"]:
            units = {k: v for k, v in layer.items() if k.endswith("Units")}
            assert not any(str(v).startswith("@@=") for v in units.values()), units
