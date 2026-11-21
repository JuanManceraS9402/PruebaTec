"""Escenario de prueba en Bogotá D.C., sobre datos geográficos reales.

Geocercas (``data/bogota_open_data.geojson``, generado por
``scripts/fetch_bogota_open_data.py`` desde datos abiertos de Bogotá):

* Perímetro urbano oficial del POT — Secretaría Distrital de Planeación (CC BY 4.0).
* Zonas escolares: 100 m alrededor de cada una de las ~2.200 sedes de
  "Colegios Bogotá D.C." — Secretaría de Educación del Distrito (CC BY-SA 4.0).
* Alta siniestralidad: tramos y puntos críticos de siniestralidad de actores
  vulnerables en vías arteriales 2022–2025 — Oficina de Seguridad Vial,
  Secretaría Distrital de Movilidad (CC BY 4.0).

Rutas y corredores (``data/bogota_osm.geojson``): calculados con OSRM sobre la red
vial de OpenStreetMap (© colaboradores de OpenStreetMap, ODbL).

El único elemento ficticio es el patio logístico de la empresa simulada.

Límites de velocidad de Bogotá: 50 km/h en toda la ciudad, incluidas autopistas
y vías principales (Ley 2251 de 2022), y 30 km/h en zonas escolares. Los
corredores y la alta siniestralidad mantienen 50: su valor está en que identifican
la zona, lo que permite agravar la severidad del exceso (ver
``rules.speed.ESCALATING_ZONES``) y analizar las infracciones por zona en la capa
Oro. El patio usa 20 km/h, una política interna de la empresa. Fuera del
perímetro (R02 llega a Soacha) aplica el límite por defecto de los parámetros.

Rutas (todas salen del patio de Fontibón):
  R01  Fontibón → Calle 26 → Calle 80 → Autopista Norte (Portal Norte)
  R02  Fontibón → Av. Boyacá → Autopista Sur → Soacha (sale del perímetro)
  R03  Fontibón → Calle 26 → Teusaquillo → Centro
  R04  Fontibón → Av. Américas → NQS → Centro        (tramos críticos de Américas y NQS)
  R05  Fontibón → Av. Ciudad de Cali → Kennedy → Bosa (tramos críticos de Cali y Bosa)
  R06  Fontibón → Av. Boyacá → Calle 80 → Suba       (Calle 72 y puntos críticos de Cali)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from shapely.geometry import LineString, shape

from simon_geo.domain import Geofence, Vehicle, ZoneType
from simon_geo.simulator.fleet import DRIVER_PROFILES, SimulatedVehicle
from simon_geo.spatial.crs import circle, corridor
from simon_geo.spatial.routes import PlannedRoute

OSM_DATA: Final = Path(__file__).with_name("data") / "bogota_osm.geojson"
OPEN_DATA: Final = Path(__file__).with_name("data") / "bogota_open_data.geojson"


# ------------------------------------------------------------ datos de OSM
@lru_cache(maxsize=1)
def _osm() -> dict[tuple[str, str], Any]:
    """Geometrías de ``bogota_osm.geojson`` indexadas por (kind, code)."""
    data = json.loads(OSM_DATA.read_text(encoding="utf-8"))
    return {
        (f["properties"]["kind"], f["properties"]["code"]): shape(f["geometry"])
        for f in data["features"]
    }


def _route_line(code: str) -> LineString:
    return _osm()[("route", code)]


# ---------------------------------------------------------------- geocercas
_KIND_TO_ZONE: Final = {
    "urban": (ZoneType.URBAN, 50),
    "school": (ZoneType.SCHOOL, 30),
    "high_risk": (ZoneType.HIGH_RISK, 50),
}

DEPOT_LONLAT: Final = _route_line("R01").coords[0]


@lru_cache(maxsize=1)
def _open_data() -> tuple[tuple[dict[str, Any], Any], ...]:
    data = json.loads(OPEN_DATA.read_text(encoding="utf-8"))
    return tuple((f["properties"], shape(f["geometry"])) for f in data["features"])


def geofences() -> list[Geofence]:
    """Geocercas con IDs estables (se insertan en este orden en ``ref.geofence``):
    primero las del escenario y luego las de datos abiertos, en el orden del archivo."""
    fences = [
        Geofence(
            2,
            "BOG-AUTONORTE",
            "Corredor Autopista Norte",
            ZoneType.HIGHWAY,
            50,
            corridor(_osm()[("corridor", "AUTONORTE")].coords, half_width_m=120),
        ),
        Geofence(
            3,
            "BOG-AUTOSUR",
            "Corredor Autopista Sur",
            ZoneType.HIGHWAY,
            50,
            corridor(_osm()[("corridor", "AUTOSUR")].coords, half_width_m=150),
        ),
        Geofence(
            4,
            "BOG-PATIO-FONTIBON",
            "Patio logístico Fontibón (ficticio)",
            ZoneType.DEPOT,
            20,
            circle(*DEPOT_LONLAT, radius_m=250),
        ),
    ]
    next_id = 5
    for props, geom in _open_data():
        zone_type, limit = _KIND_TO_ZONE[props["kind"]]
        if props["kind"] == "urban":
            fences.append(Geofence(1, props["code"], props["name"], zone_type, limit, geom))
            continue
        fences.append(Geofence(next_id, props["code"], props["name"], zone_type, limit, geom))
        next_id += 1
    return sorted(fences, key=lambda g: g.geofence_id)


def routes() -> list[PlannedRoute]:
    return [
        PlannedRoute(1, "R01", "Fontibón → Calle 26 → Autopista Norte", _route_line("R01")),
        PlannedRoute(
            2, "R02", "Fontibón → Av. Boyacá → Autopista Sur → Soacha", _route_line("R02")
        ),
        PlannedRoute(3, "R03", "Fontibón → Calle 26 → Teusaquillo → Centro", _route_line("R03")),
        PlannedRoute(4, "R04", "Fontibón → Av. Américas → NQS → Centro", _route_line("R04")),
        PlannedRoute(
            5, "R05", "Fontibón → Av. Ciudad de Cali → Kennedy → Bosa", _route_line("R05")
        ),
        PlannedRoute(6, "R06", "Fontibón → Av. Boyacá → Calle 80 → Suba", _route_line("R06")),
    ]


def vehicles() -> list[Vehicle]:
    return [
        Vehicle("VHC-8472-ABC", fuel_tank_capacity_l=200, nominal_consumption_l_100km=32),
        Vehicle("VHC-1935-BOG", fuel_tank_capacity_l=200, nominal_consumption_l_100km=35),
        Vehicle("VHC-5521-KLM", fuel_tank_capacity_l=120, nominal_consumption_l_100km=22),
        Vehicle("VHC-7310-TRZ", fuel_tank_capacity_l=150, nominal_consumption_l_100km=38),
        Vehicle("VHC-2048-QWE", fuel_tank_capacity_l=200, nominal_consumption_l_100km=30),
        Vehicle("VHC-6194-PLM", fuel_tank_capacity_l=200, nominal_consumption_l_100km=34),
        Vehicle("VHC-3371-MTO", fuel_tank_capacity_l=45, nominal_consumption_l_100km=9),
        Vehicle("VHC-4826-JKT", fuel_tank_capacity_l=120, nominal_consumption_l_100km=24),
        Vehicle("VHC-9053-HRD", fuel_tank_capacity_l=150, nominal_consumption_l_100km=28),
        Vehicle("VHC-2715-SUB", fuel_tank_capacity_l=200, nominal_consumption_l_100km=31),
    ]


def scenario(start_utc: datetime) -> list[SimulatedVehicle]:
    """Escenario de demo: cada vehículo ejercita una regla distinta."""
    v = {veh.vehicle_id: veh for veh in vehicles()}
    r = {route.code: route for route in routes()}
    return [
        # Conductor cumplido: referencia de buen Driver Score
        SimulatedVehicle(v["VHC-8472-ABC"], r["R01"], DRIVER_PROFILES["compliant"], 70, start_utc),
        # Agresivo por la Autopista Sur: excesos en zona de alta siniestralidad
        SimulatedVehicle(
            v["VHC-1935-BOG"],
            r["R02"],
            DRIVER_PROFILES["aggressive"],
            80,
            start_utc + timedelta(minutes=4),
        ),
        # Moderado que cruza la zona escolar: exceso escalado por zona
        SimulatedVehicle(
            v["VHC-5521-KLM"],
            r["R03"],
            DRIVER_PROFILES["moderate"],
            60,
            start_utc + timedelta(minutes=7),
        ),
        # Sale con el tanque casi vacío: alerta predictiva de combustible
        SimulatedVehicle(
            v["VHC-7310-TRZ"],
            r["R02"],
            DRIVER_PROFILES["compliant"],
            3.5,
            start_utc + timedelta(minutes=10),
        ),
        # Tanquea a mitad de ruta: valida la detección de recargas
        SimulatedVehicle(
            v["VHC-2048-QWE"],
            r["R01"],
            DRIVER_PROFILES["moderate"],
            20,
            start_utc + timedelta(minutes=12),
            refuel_at_km=6.0,
        ),
        # --- Rutas por tramos críticos de siniestralidad (SDM 2022-2025) ---
        # Agresivo por Av. Américas y NQS: ~9 km de tramos críticos oficiales
        SimulatedVehicle(
            v["VHC-6194-PLM"],
            r["R04"],
            DRIVER_PROFILES["aggressive"],
            75,
            start_utc + timedelta(minutes=15),
        ),
        # Vehículo liviano (45 L, 9 L/100 km) moderado por la misma ruta
        SimulatedVehicle(
            v["VHC-3371-MTO"],
            r["R04"],
            DRIVER_PROFILES["moderate"],
            60,
            start_utc + timedelta(minutes=22),
        ),
        # Agresivo por Av. Ciudad de Cali y Av. Bosa
        SimulatedVehicle(
            v["VHC-4826-JKT"],
            r["R05"],
            DRIVER_PROFILES["aggressive"],
            65,
            start_utc + timedelta(minutes=18),
        ),
        # Cumplido por la misma ruta: mismo recorrido, Driver Score opuesto
        SimulatedVehicle(
            v["VHC-9053-HRD"],
            r["R05"],
            DRIVER_PROFILES["compliant"],
            80,
            start_utc + timedelta(minutes=26),
        ),
        # Moderado por Av. Boyacá, Calle 80 y Suba
        SimulatedVehicle(
            v["VHC-2715-SUB"],
            r["R06"],
            DRIVER_PROFILES["moderate"],
            55,
            start_utc + timedelta(minutes=20),
        ),
    ]
