from __future__ import annotations

import math

import pytest

from simon_geo.rules.kinematics import segment_metrics
from simon_geo.spatial.distance import EARTH_RADIUS_M, haversine_m


class TestHaversine:
    def test_one_degree_of_latitude(self) -> None:
        expected = EARTH_RADIUS_M * math.pi / 180  # ≈ 111,195 m
        assert haversine_m(-74.0, 4.0, -74.0, 5.0) == pytest.approx(expected, rel=1e-9)

    def test_same_point_is_zero(self) -> None:
        assert haversine_m(-74.08, 4.65, -74.08, 4.65) == 0

    def test_symmetric(self) -> None:
        a = haversine_m(-74.15, 4.668, -74.048, 4.755)
        b = haversine_m(-74.048, 4.755, -74.15, 4.668)
        assert a == pytest.approx(b)

    def test_known_distance_fontibon_to_calle_170(self) -> None:
        # ~14,8 km en línea recta (verificado con geodésica WGS84, tolerancia 1 %)
        assert haversine_m(-74.15, 4.668, -74.048, 4.755) == pytest.approx(14_830, rel=0.01)


class TestSegmentMetrics:
    def test_first_point_has_no_segment(self, point_factory, params) -> None:
        m = segment_metrics(None, point_factory(), params)
        assert m.delta_s is None
        assert m.distance_m == 0
        assert not m.is_gps_outlier

    def test_normal_movement(self, point_factory, params) -> None:
        a = point_factory(seconds=0, lat=4.6500)
        b = point_factory(seconds=10, lat=4.6501)  # ~11 m en 10 s ≈ 4 km/h
        m = segment_metrics(a, b, params)
        assert m.delta_s == 10
        assert m.distance_m == pytest.approx(11.1, abs=0.2)
        assert m.implied_speed_kmh == pytest.approx(4.0, abs=0.1)
        assert not m.is_gps_outlier

    def test_impossible_jump_is_outlier(self, point_factory, params) -> None:
        a = point_factory(seconds=0, lat=4.65)
        b = point_factory(seconds=3, lat=4.68)  # 3,3 km en 3 s ≈ 4000 km/h
        m = segment_metrics(a, b, params)
        assert m.is_gps_outlier
        assert m.accel_mps2 is None  # no se calcula aceleración sobre un salto

    def test_harsh_brake_detected(self, point_factory, params) -> None:
        a = point_factory(seconds=0, speed=60)
        b = point_factory(seconds=1, speed=40)  # -5,6 m/s²
        m = segment_metrics(a, b, params)
        assert m.accel_mps2 == pytest.approx(-20 / 3.6)
        assert m.is_harsh_event

    def test_harsh_acceleration_detected(self, point_factory, params) -> None:
        a = point_factory(seconds=0, speed=0)
        b = point_factory(seconds=2, speed=25)  # +3,47 m/s²
        assert segment_metrics(a, b, params).is_harsh_event

    def test_gentle_change_is_not_harsh(self, point_factory, params) -> None:
        a = point_factory(seconds=0, speed=40)
        b = point_factory(seconds=3, speed=50)  # +0,93 m/s²
        assert not segment_metrics(a, b, params).is_harsh_event

    def test_long_gap_disables_acceleration(self, point_factory, params) -> None:
        a = point_factory(seconds=0, speed=80)
        b = point_factory(seconds=params.accel_max_gap_s + 1, speed=0)
        m = segment_metrics(a, b, params)
        assert m.accel_mps2 is None
        assert not m.is_harsh_event

    @pytest.mark.parametrize("seconds", [0, -5])
    def test_non_increasing_timestamps_rejected(self, point_factory, params, seconds) -> None:
        a = point_factory(seconds=0)
        with pytest.raises(ValueError, match="fuera de orden"):
            segment_metrics(a, point_factory(seconds=seconds), params)
