from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from gps_flota.api.app import create_app
from gps_flota.config import DbRole, Settings
from gps_flota.domain import TelemetryPoint
from tests.conftest import T0

INGEST = {"X-API-Key": "dev-ingest-key"}
ANALYST = {"X-API-Key": "dev-analyst-key"}
ADMIN = {"X-API-Key": "dev-admin-key"}


class FakeStore:
    """Imita la PK (vehicle_id, event_ts) de Bronce."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, datetime], TelemetryPoint] = {}
        self.alert_reads: list[DbRole] = []

    def insert_raw(self, points: Sequence[TelemetryPoint], source: str) -> int:
        inserted = 0
        for p in points:
            key = (p.vehicle_id, p.event_ts)
            if key not in self.rows:
                self.rows[key] = p
                inserted += 1
        return inserted

    def recent_alerts(self, role: DbRole, since: datetime, limit: int) -> list[dict[str, Any]]:
        self.alert_reads.append(role)
        masked = role is DbRole.ANALYST
        return [
            {
                "alert_id": 1,
                "vehicle_id": "VHC-****-ABC" if masked else "VHC-8472-ABC",
                "vehicle_key": "VK-0123456789ab",
                "event_ts": T0,
                "alert_type": "SPEEDING",
                "severity": "HIGH",
                "geofence_id": 4,
                "lat": 4.6,
                "lon": -74.15,
                "observed_value": 72.0,
                "threshold_value": 50.0,
                "details": {"excess_kmh": 22.0},
            }
        ]


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(store: FakeStore) -> TestClient:
    settings = Settings(_env_file=None, ingest_max_batch=10)
    app = create_app(settings=settings, writer=store, reader=store, clock=lambda: T0)
    return TestClient(app)


def packet(seconds: float = 0, **overrides: Any) -> dict[str, Any]:
    base = {
        "vehicle_id": "VHC-8472-ABC",
        "event_ts": (T0 + timedelta(seconds=seconds)).isoformat(),
        "lat": 4.668,
        "lon": -74.15,
        "speed_kmh": 42.5,
        "fuel_level_pct": 70.2,
    }
    return base | overrides


def post(client: TestClient, packets: list[dict[str, Any]], headers=INGEST):
    return client.post("/v1/telemetry", json={"packets": packets}, headers=headers)


class TestIngest:
    def test_valid_batch_is_inserted(self, client, store) -> None:
        r = post(client, [packet(0), packet(3)])
        assert r.status_code == 202
        assert r.json() == {"received": 2, "inserted": 2, "duplicates": 0, "rejected": []}
        assert len(store.rows) == 2

    def test_retransmission_is_idempotent(self, client, store) -> None:
        post(client, [packet(0)])
        r = post(client, [packet(0), packet(0)])
        assert r.json()["inserted"] == 0
        assert r.json()["duplicates"] == 2
        assert len(store.rows) == 1

    def test_timezone_offset_is_normalized(self, client, store) -> None:
        bogota = T0.astimezone(ZoneInfo("America/Bogota")).isoformat()  # mismo instante, -05:00
        assert bogota.endswith("-05:00")
        post(client, [packet(event_ts=T0.isoformat()), packet(event_ts=bogota)])
        assert len(store.rows) == 1

    def test_device_clock_in_future_rejected_individually(self, client, store) -> None:
        r = post(client, [packet(0), packet(3600)])
        body = r.json()
        assert body["inserted"] == 1
        assert [rej["index"] for rej in body["rejected"]] == [1]
        assert "futuro" in body["rejected"][0]["reason"]

    def test_too_old_rejected(self, client) -> None:
        r = post(client, [packet(-31 * 24 * 3600)])
        assert r.json()["rejected"][0]["reason"].startswith("event_ts con más de 30")

    @pytest.mark.parametrize(
        "override",
        [
            {"vehicle_id": "ABC123"},
            {"vehicle_id": "vhc-8472-abc"},
            {"event_ts": "2026-11-20T06:00:00"},  # sin zona horaria
            {"lat": 91},
            {"lon": -181},
            {"speed_kmh": -1},
            {"fuel_level_pct": 101},
            {"lat": 0, "lon": 0},  # sin fix GPS
            {"extra_field": 1},
        ],
    )
    def test_invalid_packet_rejected_with_422(self, client, store, override) -> None:
        r = post(client, [packet(**override)])
        assert r.status_code == 422
        assert not store.rows

    def test_empty_batch_rejected(self, client) -> None:
        assert post(client, []).status_code == 422

    def test_batch_size_limit(self, client) -> None:
        r = post(client, [packet(i) for i in range(11)])
        assert r.status_code == 413


class TestAuth:
    def test_missing_key(self, client) -> None:
        assert post(client, [packet()], headers={}).status_code == 401

    def test_wrong_key(self, client) -> None:
        assert post(client, [packet()], headers={"X-API-Key": "nope"}).status_code == 401

    def test_analyst_cannot_ingest(self, client) -> None:
        assert post(client, [packet()], headers=ANALYST).status_code == 403

    def test_ingest_key_cannot_read_alerts(self, client) -> None:
        assert client.get("/v1/alerts", headers=INGEST).status_code == 403


class TestAlerts:
    def test_analyst_reads_with_analyst_db_role(self, client, store) -> None:
        r = client.get("/v1/alerts", headers=ANALYST)
        assert r.status_code == 200
        assert store.alert_reads == [DbRole.ANALYST]
        assert r.json()[0]["vehicle_id"] == "VHC-****-ABC"

    def test_admin_reads_with_admin_db_role(self, client, store) -> None:
        r = client.get("/v1/alerts", headers=ADMIN)
        assert store.alert_reads == [DbRole.ADMIN]
        assert r.json()[0]["vehicle_id"] == "VHC-8472-ABC"


def test_health(client) -> None:
    assert client.get("/health").json()["status"] == "ok"
