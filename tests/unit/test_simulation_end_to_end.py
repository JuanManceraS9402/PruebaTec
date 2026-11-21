"""Pipeline en memoria completo: escenario Bogotá → simulador → enriquecimiento.

Verifica que cada vehículo del escenario dispara exactamente la regla que
debe ejercitar (ver ``simon_geo.seed.bogota.scenario``).
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from simon_geo.domain import AlertType, ZoneType
from simon_geo.parameters import PipelineParameters
from simon_geo.processing.enrichment import EnrichmentResult, SilverEnricher
from simon_geo.seed import bogota
from simon_geo.simulator.fleet import FleetSimulator, SimulationConfig
from simon_geo.spatial.geofencing import GeofenceIndex, resolve_speed_limit
from tests.conftest import T0

GEOFENCES = {g.code: g for g in bogota.geofences()}
INDEX = GeofenceIndex(GEOFENCES.values())


def simulate(seed: int = 42):
    fleet = bogota.scenario(T0)
    return list(FleetSimulator(fleet, INDEX, config=SimulationConfig(seed=seed)).run())


@pytest.fixture(scope="module")
def raw_points():
    return simulate()


@pytest.fixture(scope="module")
def result(raw_points) -> EnrichmentResult:
    fleet = bogota.scenario(T0)
    enricher = SilverEnricher(
        vehicles=bogota.vehicles(),
        geofence_index=INDEX,
        params=PipelineParameters(),
        active_routes={sv.vehicle.vehicle_id: sv.route for sv in fleet},
    )
    return enricher.enrich(raw_points)


def alerts_by_vehicle(result: EnrichmentResult):
    out = defaultdict(list)
    for a in result.alerts:
        out[a.vehicle_id].append(a)
    return out


class TestBogotaSeed:
    def test_geofence_codes_unique(self) -> None:
        assert len(GEOFENCES) == len(bogota.geofences())

    def test_depot_is_inside_urban_perimeter(self) -> None:
        codes = {g.code for g in INDEX.containing(*bogota.DEPOT_LONLAT)}
        assert {"BOG-URB", "BOG-PATIO-FONTIBON"} <= codes

    def test_loads_real_open_data(self) -> None:
        """Perímetro oficial, ~2.200 sedes escolares y los tramos/puntos críticos de la SDM."""
        by_type = defaultdict(int)
        for g in GEOFENCES.values():
            by_type[g.zone_type] += 1
        assert by_type[ZoneType.URBAN] == 1
        assert by_type[ZoneType.SCHOOL] > 2000
        assert by_type[ZoneType.HIGH_RISK] > 40
        assert GEOFENCES["BOG-URB"].geometry.area > 0.025  # ~370 km² en grados²

    def test_school_zone_limit_on_route(self) -> None:
        r03 = next(r for r in bogota.routes() if r.code == "R03")
        school = next(
            g
            for g in GEOFENCES.values()
            if g.zone_type is ZoneType.SCHOOL and g.geometry.intersects(r03.geometry)
        )
        inside = school.geometry.intersection(r03.geometry)
        piece = max(getattr(inside, "geoms", [inside]), key=lambda g: g.length)
        point = piece.interpolate(0.5, normalized=True)  # mitad del tramo: lejos del borde
        d = resolve_speed_limit(INDEX.containing(point.x, point.y), 80)
        assert d.limit_kmh == 30 and d.zone_type is ZoneType.SCHOOL

    def test_official_critical_segment_wins_over_corridor(self) -> None:
        """Mismo límite (50), pero dentro del tramo crítico oficial de la Autopista Sur
        gobierna la alta siniestralidad, que agrava la severidad del exceso."""
        overlap = GEOFENCES["SDM-TC-AUTOSUR"].geometry.intersection(
            GEOFENCES["BOG-AUTOSUR"].geometry
        )
        point = overlap.representative_point()
        d = resolve_speed_limit(INDEX.containing(point.x, point.y), 80)
        assert d.limit_kmh == 50 and d.zone_type is ZoneType.HIGH_RISK

    def test_bogota_legal_limits_are_50_or_30(self) -> None:
        """En Bogotá el límite es 50 km/h (incluidas autopistas) y 30 en zonas escolares.
        El patio es política interna de la empresa, no un límite legal."""
        for g in GEOFENCES.values():
            if g.zone_type is ZoneType.DEPOT:
                continue
            expected = 30 if g.zone_type is ZoneType.SCHOOL else 50
            assert g.speed_limit_kmh == expected, g.code

    def test_r02_leaves_urban_perimeter(self) -> None:
        r02 = next(r for r in bogota.routes() if r.code == "R02")
        end_lon, end_lat = r02.geometry.coords[-1]
        assert INDEX.containing(end_lon, end_lat) == []

    @pytest.mark.parametrize(
        ("code", "low", "high"),
        [
            ("R01", 25, 30),
            ("R02", 21, 26),
            ("R03", 16, 20),
            ("R04", 18, 22),
            ("R05", 9, 13),
            ("R06", 23, 27),
        ],
    )
    def test_route_lengths_plausible(self, code: str, low: float, high: float) -> None:
        route = next(r for r in bogota.routes() if r.code == code)
        assert low <= route.length_km <= high

    def test_corridors_follow_their_routes(self) -> None:
        """Los corredores son tramos de la ruta real: la ruta los recorre por dentro."""
        routes = {r.code: r for r in bogota.routes()}
        for code, route in (("BOG-AUTONORTE", "R01"), ("BOG-AUTOSUR", "R02")):
            inside = GEOFENCES[code].geometry.intersection(routes[route].geometry)
            assert inside.length > 0.02, code  # > ~2 km de ruta dentro del corredor

    @pytest.mark.parametrize(
        ("code", "critical"),
        [
            ("R04", {"SDM-TC-AME_W", "SDM-TC-AME_E", "SDM-TC-NQS_S"}),
            ("R05", {"SDM-TC-CALI_C", "SDM-TC-CALI_S", "SDM-TC-BOSA"}),
            ("R06", {"SDM-TC-CALI_N", "SDM-TC-CL72", "SDM-PC-P14"}),
        ],
    )
    def test_new_routes_cross_official_critical_segments(
        self, code: str, critical: set[str]
    ) -> None:
        route = next(r for r in bogota.routes() if r.code == code)
        crossed = {c for c in critical if GEOFENCES[c].geometry.intersects(route.geometry)}
        assert crossed == critical

    def test_every_route_crosses_real_school_zones(self) -> None:
        schools = [g for g in GEOFENCES.values() if g.zone_type is ZoneType.SCHOOL]
        for route in bogota.routes():
            assert any(g.geometry.intersects(route.geometry) for g in schools), route.code


class TestSimulator:
    def test_deterministic_for_same_seed(self, raw_points) -> None:
        assert simulate(42) == raw_points

    def test_different_seed_differs(self, raw_points) -> None:
        assert simulate(7) != raw_points

    def test_every_vehicle_reports_in_time_order(self, raw_points) -> None:
        last = {}
        for p in raw_points:
            assert p.event_ts >= last.get(p.vehicle_id, p.event_ts)
            last[p.vehicle_id] = p.event_ts
        assert set(last) == {v.vehicle_id for v in bogota.vehicles()}

    def test_injects_duplicates_and_outliers(self, result) -> None:
        assert result.duplicates > 0
        assert any(p.is_gps_outlier for p in result.points)


class TestEndToEnd:
    def test_all_points_processed(self, raw_points, result) -> None:
        assert len(result.points) + result.duplicates == len(raw_points)
        assert not result.unknown_vehicle

    def test_compliant_driver_has_no_speeding(self, result) -> None:
        alerts = alerts_by_vehicle(result)["VHC-8472-ABC"]
        assert not [a for a in alerts if a.alert_type is AlertType.SPEEDING]

    def test_compliant_driver_has_no_harsh_events(self, result) -> None:
        points = [p for p in result.points if p.vehicle_id == "VHC-8472-ABC"]
        assert not [p for p in points if p.is_harsh_event]

    def test_aggressive_driver_speeds_in_official_critical_zone(self, result) -> None:
        alerts = alerts_by_vehicle(result)["VHC-1935-BOG"]
        high_risk = {g.geofence_id for g in GEOFENCES.values() if g.zone_type is ZoneType.HIGH_RISK}
        assert any(
            a.alert_type is AlertType.SPEEDING and a.geofence_id in high_risk for a in alerts
        )

    def test_school_zone_speeding_detected(self, result) -> None:
        alerts = alerts_by_vehicle(result)["VHC-5521-KLM"]
        schools = {g.geofence_id for g in GEOFENCES.values() if g.zone_type is ZoneType.SCHOOL}
        assert any(a.geofence_id in schools for a in alerts)

    def test_low_fuel_vehicle_gets_predictive_alert(self, result) -> None:
        alerts = alerts_by_vehicle(result)["VHC-7310-TRZ"]
        assert any(a.alert_type is AlertType.LOW_FUEL_RANGE for a in alerts)

    def test_vehicles_with_fuel_get_no_fuel_alert(self, result) -> None:
        by_vehicle = alerts_by_vehicle(result)
        for vid in ("VHC-8472-ABC", "VHC-1935-BOG", "VHC-5521-KLM"):
            assert not [a for a in by_vehicle[vid] if a.alert_type is AlertType.LOW_FUEL_RANGE]

    def test_refuel_detected(self, result) -> None:
        refuels = [p for p in result.points if p.vehicle_id == "VHC-2048-QWE" and p.is_refuel]
        assert len(refuels) == 1

    def test_measured_consumption_close_to_simulated(self, result) -> None:
        """El consumo estimado converge al real simulado (nominal × factor del conductor)."""
        last = [p for p in result.points if p.vehicle_id == "VHC-8472-ABC"][-1]
        assert last.consumption_l_100km == pytest.approx(32 * 1.00, rel=0.10)


def test_driver_profiles_rank_by_time_speeding(result: EnrichmentResult) -> None:
    """Cumplido nunca excede, moderado excede a ratos y agresivo casi siempre."""
    profile = {sv.vehicle.vehicle_id: sv.driver.name for sv in bogota.scenario(T0)}
    for vid, name in profile.items():
        pts = [p for p in result.points if p.vehicle_id == vid and not p.is_gps_outlier]
        share = sum(p.is_speeding for p in pts) / len(pts)
        if name == "compliant":
            assert share == 0, vid
        elif name == "moderate":
            assert 0.02 < share < 0.40, (vid, share)
        else:
            assert share > 0.80, (vid, share)


def test_aggressive_drivers_get_critical_alerts_in_official_critical_zones(
    result: EnrichmentResult,
) -> None:
    """En los tramos críticos de la SDM la severidad sube un nivel: los agresivos de las
    rutas nuevas llegan a CRITICAL ahí."""
    high_risk = {g.geofence_id for g in GEOFENCES.values() if g.zone_type is ZoneType.HIGH_RISK}
    for vid in ("VHC-6194-PLM", "VHC-4826-JKT"):
        assert any(
            a.vehicle_id == vid and a.geofence_id in high_risk and a.severity.value == "CRITICAL"
            for a in result.alerts
        ), vid


def test_light_vehicle_has_enough_fuel(result: EnrichmentResult) -> None:
    assert not [
        a
        for a in result.alerts
        if a.vehicle_id == "VHC-3371-MTO" and a.alert_type is AlertType.LOW_FUEL_RANGE
    ]
