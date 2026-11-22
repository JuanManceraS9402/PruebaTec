"""Lógica del worker incremental sin base de datos.

Lo crítico es la ventana de lookback: un lote nuevo debe continuar el estado
de la traza (episodios de exceso, odómetro, consumo) y no empezar de cero.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from gps_flota.domain import AlertType, Severity, ZoneType
from gps_flota.processing.enrichment import SilverEnricher
from gps_flota.processing.worker import (
    BronzeToSilverWorker,
    WorkerConfig,
    alert_row,
    enrich_window,
    silver_row,
)
from gps_flota.spatial.geofencing import GeofenceIndex
from tests.conftest import T0, make_geofence, square

URBAN = make_geofence(7, ZoneType.URBAN, 50, square(-74.08, 4.65, 0.05))
INDEX = GeofenceIndex([URBAN])


@pytest.fixture
def enricher_factory(vehicle, params):
    return lambda: SilverEnricher([vehicle], INDEX, params)


@pytest.fixture
def speeding_track(point_factory):
    """20 reportes cada 10 s a 70 km/h en zona de 50: un único episodio de exceso."""
    return [point_factory(seconds=10 * i, lon=-74.08 + 0.0002 * i, speed=70.0) for i in range(20)]


EMIT_FROM = T0 + timedelta(seconds=100)  # el "lote nuevo" empieza en el reporte 10


class TestEnrichWindow:
    def test_only_points_from_emit_from_are_returned(self, enricher_factory, speeding_track):
        result = enrich_window(enricher_factory(), speeding_track, None, EMIT_FROM)
        assert [p.event_ts for p in result.points] == [
            p.event_ts for p in speeding_track if p.event_ts >= EMIT_FROM
        ]

    def test_lookback_continues_episode_without_repeating_alert(
        self, enricher_factory, speeding_track
    ):
        """El episodio empezó antes del lote: con lookback no hay alerta nueva."""
        result = enrich_window(enricher_factory(), speeding_track, None, EMIT_FROM)
        assert result.alerts == []
        assert all(p.is_speeding for p in result.points)

    def test_without_lookback_the_episode_would_be_alerted_again(
        self, enricher_factory, speeding_track
    ):
        """Contraprueba: procesar solo el lote nuevo duplica la alerta del episodio."""
        new_only = [p for p in speeding_track if p.event_ts >= EMIT_FROM]
        result = enrich_window(enricher_factory(), new_only, None, EMIT_FROM)
        assert [a.event_ts for a in result.alerts] == [EMIT_FROM]

    def test_lookback_keeps_odometer_continuous(self, enricher_factory, speeding_track):
        """El primer punto emitido tiene distancia de segmento (hay punto previo)."""
        result = enrich_window(enricher_factory(), speeding_track, None, EMIT_FROM)
        first = result.points[0]
        assert first.prev_event_ts == EMIT_FROM - timedelta(seconds=10)
        assert first.segment_distance_m > 0

    def test_matches_single_pass_processing(self, enricher_factory, speeding_track):
        """Ventana + filtro produce exactamente lo mismo que procesar todo de una vez."""
        single = enricher_factory().enrich(speeding_track)
        windowed = enrich_window(enricher_factory(), speeding_track, None, EMIT_FROM)
        assert windowed.points == [p for p in single.points if p.event_ts >= EMIT_FROM]
        assert windowed.alerts == [a for a in single.alerts if a.event_ts >= EMIT_FROM]

    def test_uses_postgis_geofences_when_given(self, enricher_factory, speeding_track):
        """Si PostGIS dice que ningún punto está en geocerca, rige el límite por defecto."""
        outside = {(p.vehicle_id, p.event_ts): () for p in speeding_track}
        result = enrich_window(enricher_factory(), speeding_track, outside, EMIT_FROM)
        assert all(p.geofence_ids == () for p in result.points)
        assert all(p.applied_speed_limit_kmh == 80 for p in result.points)
        assert not any(p.is_speeding for p in result.points)

    def test_unknown_vehicle_points_are_reported(self, params, point_factory):
        enricher = SilverEnricher([], INDEX, params)
        points = [point_factory(seconds=s) for s in (0, 200)]
        result = enrich_window(enricher, points, None, EMIT_FROM)
        assert result.points == []
        assert [p.event_ts for p in result.unknown_vehicle] == [T0 + timedelta(seconds=200)]


class TestRowMapping:
    def test_silver_row(self, enricher_factory, speeding_track):
        point = enricher_factory().enrich(speeding_track).points[-1]
        row = silver_row(point)
        assert row["geofence_ids"] == [7]
        assert row["applied_speed_limit_kmh"] == 50
        assert isinstance(row["applied_speed_limit_kmh"], int)
        assert row["lon"] == point.lon and row["lat"] == point.lat
        assert row["is_speeding"] is True

    def test_alert_row(self, enricher_factory, speeding_track):
        alert = enricher_factory().enrich(speeding_track).alerts[0]
        row = alert_row(alert)
        assert row["alert_type"] == AlertType.SPEEDING.value
        assert row["severity"] in {s.value for s in Severity}
        assert row["geofence_id"] == 7
        assert isinstance(row["details"], Jsonb)
        assert row["details"].obj["geofence_code"] == URBAN.code


class TestWorkerConfig:
    def test_negative_lookback_rejected(self):
        with pytest.raises(ValueError):
            WorkerConfig(lookback=timedelta(minutes=-1))

    def test_worker_requires_transactional_connection(self):
        with pytest.raises(ValueError, match="autocommit"):
            BronzeToSilverWorker(SimpleNamespace(autocommit=True))
