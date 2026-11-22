from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from gps_flota.domain import Geofence, ZoneType
from gps_flota.spatial.geofencing import GeofenceIndex, resolve_speed_limit
from tests.conftest import make_geofence, square

URBAN = make_geofence(1, ZoneType.URBAN, 50, square(-74.08, 4.65, 0.05))
SCHOOL = make_geofence(2, ZoneType.SCHOOL, 30, square(-74.08, 4.65, 0.002))
HIGHWAY = make_geofence(3, ZoneType.HIGHWAY, 60, square(-74.05, 4.65, 0.005))
MONITOR_ONLY = make_geofence(4, ZoneType.RESTRICTED, None, square(-74.10, 4.62, 0.005))


class TestGeofenceIndex:
    def test_point_inside_returns_geofence(self) -> None:
        index = GeofenceIndex([URBAN])
        assert index.containing(-74.08, 4.65) == [URBAN]

    def test_point_outside_returns_empty(self) -> None:
        index = GeofenceIndex([URBAN])
        assert index.containing(-73.0, 4.65) == []

    def test_point_on_boundary_counts_as_inside(self) -> None:
        """Semántica ST_Intersects: el borde pertenece a la geocerca."""
        index = GeofenceIndex([URBAN])
        assert index.containing(-74.08 + 0.05, 4.65) == [URBAN]

    def test_overlapping_geofences_all_returned_sorted_by_id(self) -> None:
        index = GeofenceIndex([SCHOOL, URBAN])
        assert index.containing(-74.08, 4.65) == [URBAN, SCHOOL]

    def test_empty_index(self) -> None:
        assert GeofenceIndex([]).containing(-74.08, 4.65) == []

    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicados"):
            GeofenceIndex([URBAN, make_geofence(1, ZoneType.SCHOOL, 30, square(0, 0, 1))])


class TestGeofenceValidation:
    def test_self_intersecting_polygon_rejected(self) -> None:
        bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
        with pytest.raises(ValueError, match="inválida"):
            make_geofence(9, ZoneType.URBAN, 50, bowtie)

    def test_non_polygon_rejected(self) -> None:
        from shapely.geometry import Point

        with pytest.raises(ValueError, match="Polygon"):
            Geofence(9, "X", "X", ZoneType.URBAN, 50, Point(0, 0))  # type: ignore[arg-type]

    @pytest.mark.parametrize("limit", [0, 4, 131])
    def test_limit_out_of_range_rejected(self, limit: int) -> None:
        with pytest.raises(ValueError, match="Límite"):
            make_geofence(9, ZoneType.URBAN, limit, square(0, 0, 1))


class TestResolveSpeedLimit:
    def test_no_geofence_uses_default(self) -> None:
        decision = resolve_speed_limit([], default_limit_kmh=80)
        assert decision.limit_kmh == 80
        assert decision.governing is None

    def test_geofence_without_limit_is_ignored(self) -> None:
        decision = resolve_speed_limit([MONITOR_ONLY], default_limit_kmh=80)
        assert decision.limit_kmh == 80

    def test_school_inside_urban_applies_school_limit(self) -> None:
        decision = resolve_speed_limit([URBAN, SCHOOL], default_limit_kmh=80)
        assert decision.limit_kmh == 30
        assert decision.governing == SCHOOL

    def test_more_specific_zone_wins_even_with_higher_limit(self) -> None:
        """Un corredor rápido dentro del perímetro urbano permite 60, no 50."""
        decision = resolve_speed_limit([URBAN, HIGHWAY], default_limit_kmh=80)
        assert decision.limit_kmh == 60
        assert decision.zone_type is ZoneType.HIGHWAY

    def test_same_priority_takes_most_restrictive(self) -> None:
        other_urban = make_geofence(7, ZoneType.URBAN, 40, square(-74.08, 4.65, 0.01))
        decision = resolve_speed_limit([URBAN, other_urban], default_limit_kmh=80)
        assert decision.limit_kmh == 40

    def test_order_of_input_does_not_matter(self) -> None:
        a = resolve_speed_limit([URBAN, SCHOOL, HIGHWAY], 80)
        b = resolve_speed_limit([HIGHWAY, SCHOOL, URBAN], 80)
        assert a == b
