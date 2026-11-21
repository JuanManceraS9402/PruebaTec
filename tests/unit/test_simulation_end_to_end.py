"""Pipeline en memoria completo: escenario Bogotá → simulador → enriquecimiento.

Verifica que cada vehículo del escenario dispara exactamente la regla que
debe ejercitar (ver ``simon_geo.seed.bogota.scenario``).
"""

from __future__ import annotations

from collections import defaultdict

import pytest
from shapely.geometry import Point

from simon_geo.domain import AlertType, ZoneType
from simon_geo.parameters import PipelineParameters
from simon_geo.processing.enrichment import EnrichmentResult, SilverEnricher
from simon_geo.seed import bogota
from simon_geo.simulator.fleet import FleetSimulator, SimulationConfig
from simon_geo.spatial.crs import lonlat_to_metric
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

    def test_school_zone_limit(self) -> None:
        d = resolve_speed_limit(INDEX.containing(*bogota.SCHOOL_LONLAT), 80)
        assert d.limit_kmh == 30 and d.zone_type is ZoneType.SCHOOL

    def test_high_risk_wins_over_highway_at_calle_80(self) -> None:
        """Mismo límite (50), pero la zona que gobierna es la de alta siniestralidad,
        por lo que un exceso ahí se califica con más severidad."""
        d = resolve_speed_limit(INDEX.containing(*bogota.CALLE80_LONLAT), 80)
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
        ("code", "low", "high"), [("R01", 25, 30), ("R02", 21, 26), ("R03", 16, 20)]
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

    def test_landmarks_lie_on_their_routes(self) -> None:
        routes = {r.code: r for r in bogota.routes()}
        assert routes["R03"].remaining_km(*bogota.SCHOOL_LONLAT) < routes["R03"].length_km
        assert (
            routes["R01"].metric_geometry.distance(Point(lonlat_to_metric(*bogota.CALLE80_LONLAT)))
            < 1.0
        )


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

    def test_aggressive_driver_speeds_in_autopista_sur(self, result) -> None:
        alerts = alerts_by_vehicle(result)["VHC-1935-BOG"]
        autosur = GEOFENCES["BOG-AUTOSUR"].geofence_id
        assert any(a.alert_type is AlertType.SPEEDING and a.geofence_id == autosur for a in alerts)

    def test_school_zone_speeding_detected(self, result) -> None:
        alerts = alerts_by_vehicle(result)["VHC-5521-KLM"]
        school = GEOFENCES["BOG-ESC-TEUSAQUILLO"].geofence_id
        assert any(a.geofence_id == school for a in alerts)

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
