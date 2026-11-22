from __future__ import annotations

import pytest
from shapely.geometry import LineString

from gps_flota.domain import AlertType, Severity, Vehicle, ZoneType
from gps_flota.rules.track import MAX_CONSECUTIVE_OUTLIERS, TrackProcessor
from gps_flota.spatial.geofencing import GeofenceIndex
from gps_flota.spatial.routes import PlannedRoute
from tests.conftest import make_geofence, square

URBAN = make_geofence(1, ZoneType.URBAN, 50, square(-74.10, 4.65, 0.2))
INDEX = GeofenceIndex([URBAN])
ROUTE = PlannedRoute(1, "T", "Test", LineString([(-74.10, 4.60), (-74.10, 4.70)]))  # ~11 km

STEP_DEG = 0.0003  # ~33 m: a 3 s por reporte ≈ 40 km/h


def walk(point_factory, speeds, *, fuel=60.0, start_lat=4.60):
    """Puntos cada 3 s avanzando hacia el norte con las velocidades dadas."""
    return [
        point_factory(seconds=3 * i, lat=start_lat + STEP_DEG * i, lon=-74.10, speed=s, fuel=fuel)
        for i, s in enumerate(speeds)
    ]


def run(processor, points):
    results = [processor.process(p) for p in points]
    return [r.point for r in results], [a for r in results for a in r.alerts]


class TestSpeedingEpisodes:
    def test_one_alert_per_episode(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        _, alerts = run(proc, walk(point_factory, [40, 60, 62, 61, 60, 40]))
        assert [a.alert_type for a in alerts] == [AlertType.SPEEDING]
        assert alerts[0].severity is Severity.MEDIUM
        assert alerts[0].threshold_value == 50
        assert alerts[0].geofence_id == URBAN.geofence_id

    def test_escalation_within_episode_emits_new_alert(
        self, point_factory, vehicle, params
    ) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        _, alerts = run(proc, walk(point_factory, [60, 75, 76, 62]))
        assert [a.severity for a in alerts] == [Severity.MEDIUM, Severity.HIGH]

    def test_new_episode_after_slowing_down(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        _, alerts = run(proc, walk(point_factory, [60, 60, 45, 60]))
        assert len(alerts) == 2


class TestOutliers:
    def test_single_jump_is_flagged_and_does_not_break_the_track(
        self, point_factory, vehicle, params
    ) -> None:
        pts = walk(point_factory, [40] * 5)
        pts[2] = point_factory(seconds=6, lat=4.65, lon=-74.10, speed=40)  # salto de ~5,5 km
        proc = TrackProcessor(vehicle, INDEX, params)
        enriched, _ = run(proc, pts)
        assert [e.is_gps_outlier for e in enriched] == [False, False, True, False, False]
        # el punto tras el salto se mide contra el último válido (2 pasos ≈ 67 m)
        assert enriched[3].segment_distance_m == pytest.approx(66.7, abs=1)
        # el odómetro no incluye el salto de ~5,5 km
        assert proc.odometer_km == pytest.approx(4 * 0.0334, abs=0.005)

    def test_reanchors_after_consecutive_outliers(self, point_factory, vehicle, params) -> None:
        """Si el primer punto era el erróneo, la traza se recupera."""
        bad_first = point_factory(seconds=0, lat=4.68)
        rest = walk(point_factory, [40] * 5)[1:]
        proc = TrackProcessor(vehicle, INDEX, params)
        enriched, _ = run(proc, [bad_first, *rest])
        flags = [e.is_gps_outlier for e in enriched]
        assert flags == [False] + [True] * (MAX_CONSECUTIVE_OUTLIERS - 1) + [False, False]

    def test_outlier_uses_last_valid_position_for_geofence(
        self, point_factory, vehicle, params
    ) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        proc.process(point_factory(seconds=0, lat=4.60, lon=-74.10))
        far_away = point_factory(seconds=3, lat=4.60, lon=-73.0, speed=70)  # fuera de URBAN
        out = proc.process(far_away).point
        assert out.is_gps_outlier
        assert out.geofence_ids == (URBAN.geofence_id,)
        assert out.applied_speed_limit_kmh == 50


class TestFuel:
    def test_low_fuel_raises_single_critical_alert(self, point_factory, vehicle, params) -> None:
        # 3 % de 100 L = 3 L a 30 L/100 km → 10 km de autonomía para ~11 km de ruta
        proc = TrackProcessor(vehicle, INDEX, params, route=ROUTE)
        enriched, alerts = run(proc, walk(point_factory, [40] * 5, fuel=3.0))
        fuel_alerts = [a for a in alerts if a.alert_type is AlertType.LOW_FUEL_RANGE]
        assert len(fuel_alerts) == 1
        assert fuel_alerts[0].severity is Severity.CRITICAL
        assert fuel_alerts[0].observed_value == pytest.approx(10, abs=0.01)
        assert all(e.is_low_fuel_risk for e in enriched)

    @staticmethod
    def fuel_track(point_factory, levels):
        return [
            point_factory(seconds=3 * i, lat=4.60 + 0.00001 * i, lon=-74.10, speed=10, fuel=f)
            for i, f in enumerate(levels)
        ]

    @staticmethod
    def fuel_alerts(alerts):
        return [a for a in alerts if a.alert_type is AlertType.LOW_FUEL_RANGE]

    def test_noise_around_threshold_keeps_one_alert(self, point_factory, vehicle, params) -> None:
        """Ruta ~11 km → requeridos ~12,7. El nivel oscila entre 12,3 y 13,0 km de autonomía."""
        proc = TrackProcessor(vehicle, INDEX, params, route=ROUTE)
        enriched, alerts = run(proc, self.fuel_track(point_factory, [3.7, 3.9] * 20))
        assert len(self.fuel_alerts(alerts)) == 1
        # el indicador por punto sigue reflejando el estado real de cada reporte
        assert {e.is_low_fuel_risk for e in enriched} == {True, False}

    def test_clear_recovery_starts_a_new_episode(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params, route=ROUTE)
        # 3,7 % = riesgo; >3 km sobre el margen cierran el episodio; 2,5 % vuelve al riesgo
        levels = [3.7] + [4.5] * 100 + [2.5]
        points = [
            point_factory(seconds=3 * i, lat=4.60 + STEP_DEG * i, lon=-74.10, speed=40, fuel=f)
            for i, f in enumerate(levels)
        ]
        _, alerts = run(proc, points)
        assert [a.severity for a in self.fuel_alerts(alerts)] == [Severity.HIGH, Severity.HIGH]

    def test_short_recovery_does_not_close_episode(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params, route=ROUTE)
        levels = [3.7] + [4.5] * 20 + [3.7]  # ~0,7 km sobre el margen: es ruido, no recuperación
        points = [
            point_factory(seconds=3 * i, lat=4.60 + STEP_DEG * i, lon=-74.10, speed=40, fuel=f)
            for i, f in enumerate(levels)
        ]
        _, alerts = run(proc, points)
        assert len(self.fuel_alerts(alerts)) == 1

    def test_escalation_alerts_once_despite_noise(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params, route=ROUTE)
        _, alerts = run(proc, self.fuel_track(point_factory, [3.7, 3.0, 3.7, 3.0, 3.7]))
        assert [a.severity for a in self.fuel_alerts(alerts)] == [
            Severity.HIGH,
            Severity.CRITICAL,
        ]

    def test_refuel_closes_episode(self, point_factory, params) -> None:
        small = Vehicle("VHC-8472-ABC", fuel_tank_capacity_l=20, nominal_consumption_l_100km=30)
        proc = TrackProcessor(small, INDEX, params, route=ROUTE)
        # Tras tanquear sigue sin alcanzar (6,2 % de 20 L ≈ 4 km): es un episodio nuevo
        _, alerts = run(proc, self.fuel_track(point_factory, [1.0, 1.0, 6.2]))
        assert len(self.fuel_alerts(alerts)) == 2

    def test_enough_fuel_no_alert(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params, route=ROUTE)
        enriched, alerts = run(proc, walk(point_factory, [40] * 5, fuel=60.0))
        assert not alerts
        assert enriched[-1].remaining_route_km == pytest.approx(11.06 - 0.13, abs=0.05)

    def test_no_route_means_no_fuel_assessment(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        enriched, alerts = run(proc, walk(point_factory, [40] * 3, fuel=1.0))
        assert not alerts
        assert enriched[-1].remaining_route_km is None

    def test_refuel_detected(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        pts = walk(point_factory, [0, 0, 0], fuel=10.0)
        pts[2] = point_factory(seconds=6, lat=pts[2].lat, lon=-74.10, speed=0, fuel=95.0)
        enriched, _ = run(proc, pts)
        assert [e.is_refuel for e in enriched] == [False, False, True]
        assert enriched[2].fuel_delta_pct == pytest.approx(85)


class TestValidation:
    def test_out_of_order_rejected(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        proc.process(point_factory(seconds=10))
        with pytest.raises(ValueError, match="fuera de orden"):
            proc.process(point_factory(seconds=5))

    def test_other_vehicle_rejected(self, point_factory, vehicle, params) -> None:
        proc = TrackProcessor(vehicle, INDEX, params)
        with pytest.raises(ValueError, match="procesador"):
            proc.process(point_factory(vehicle_id="VHC-0000-XYZ"))
