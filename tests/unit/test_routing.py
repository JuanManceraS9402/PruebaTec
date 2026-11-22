"""Rutas por puntos en el mapa: clics, área de servicio y fallos de OSRM."""

from __future__ import annotations

import httpx
import pytest

from gps_flota.routing import Place, RoutingError, in_service_area, next_points, plan_route

LINE = [[-74.15, 4.668], [-74.12, 4.68], [-74.09, 4.70]]
A = Place(-74.15, 4.668)
B = Place(-74.09, 4.70)
C = Place(-74.10, 4.60)


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def osrm(coords=None, waypoints=None) -> httpx.Client:
    body = {"code": "Ok", "routes": [{"geometry": {"coordinates": coords or LINE}}]}
    if waypoints:
        body["waypoints"] = waypoints
    return client_for(lambda _: httpx.Response(200, json=body))


def test_clicks_set_start_then_end_then_restart() -> None:
    assert next_points([], A) == [A]
    assert next_points([A], B) == [A, B]
    assert next_points([A, B], C) == [C]


def test_service_area() -> None:
    assert in_service_area(A)
    assert not in_service_area(Place(-75.56, 6.25))  # Medellín


def test_route_is_named_after_its_roads() -> None:
    plan = plan_route(A, B, client=osrm(waypoints=[{"name": "Calle 26"}, {"name": "Carrera 7"}]))
    assert plan.name == "Calle 26 → Carrera 7"
    assert 5 < plan.length_km < 12
    route = plan.planned("SBX-VHC-1234-XYZ")
    assert route.code == "SBX-VHC-1234-XYZ" and route.name == plan.name


def test_unnamed_roads_fall_back_to_coordinates() -> None:
    plan = plan_route(A, B, client=osrm(waypoints=[{"name": ""}, {"name": ""}]))
    assert plan.name == "4.6680, -74.1500 → 4.7000, -74.0900"


def test_outside_bogota_is_rejected_without_calling_the_service() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no debe llamar a OSRM")

    with pytest.raises(RoutingError, match="fuera"):
        plan_route(Place(-75.56, 6.25), B, client=client_for(fail))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_service_failure_is_explained(status) -> None:
    with pytest.raises(RoutingError, match="no responde"):
        plan_route(A, B, client=client_for(lambda _: httpx.Response(status)))


def test_no_road_between_points() -> None:
    client = client_for(lambda _: httpx.Response(200, json={"code": "NoRoute"}))
    with pytest.raises(RoutingError, match="recorrido"):
        plan_route(A, B, client=client)


def test_too_short_route_is_rejected() -> None:
    with pytest.raises(RoutingError, match="corta"):
        plan_route(A, B, client=osrm([[-74.15, 4.668], [-74.1499, 4.6681]]))
