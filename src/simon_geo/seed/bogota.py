"""Escenario de prueba en Bogotá D.C.

Geometrías APROXIMADAS construidas a mano para la demo (no son cartografía
oficial). Límites de velocidad de Bogotá: 50 km/h en toda la ciudad, incluidas
autopistas y vías principales (Ley 2251 de 2022), y 30 km/h en zonas escolares.

Las geocercas de corredor y de alta siniestralidad mantienen el límite de 50; su
valor está en que identifican la zona, lo que permite agravar la severidad del
exceso (ver ``rules.speed.ESCALATING_ZONES``) y analizar las infracciones por
corredor en la capa Oro. El patio logístico usa 20 km/h, que es una política
interna de la empresa y no un límite legal.

Rutas (todas salen del patio de Fontibón):
  R01  Fontibón → Calle 26 → Autopista Norte (Calle 170)
  R02  Fontibón → Av. Boyacá → Autopista Sur → salida a Soacha (sale del perímetro)
  R03  Fontibón → Calle 26 → Teusaquillo (zona escolar) → Centro
"""

from __future__ import annotations

from datetime import datetime, timedelta

from shapely.geometry import LineString, Polygon

from simon_geo.domain import Geofence, Vehicle, ZoneType
from simon_geo.simulator.fleet import DRIVER_PROFILES, SimulatedVehicle
from simon_geo.spatial.crs import circle, corridor
from simon_geo.spatial.routes import PlannedRoute

# --------------------------------------------------------------------- rutas
_R01 = [
    (-74.1500, 4.6680),
    (-74.1200, 4.6600),
    (-74.0950, 4.6480),
    (-74.0700, 4.6550),
    (-74.0600, 4.6800),
    (-74.0580, 4.7000),
    (-74.0520, 4.7450),
    (-74.0480, 4.7550),
]

_R02 = [
    (-74.1500, 4.6680),
    (-74.1400, 4.6400),
    (-74.1400, 4.6150),
    (-74.1550, 4.6000),
    (-74.1800, 4.5900),
    (-74.2000, 4.5850),
    (-74.2150, 4.5800),
]

_R03 = [
    (-74.1500, 4.6680),
    (-74.1100, 4.6500),
    (-74.0850, 4.6370),
    (-74.0720, 4.6150),
    (-74.0650, 4.6000),
]

# ---------------------------------------------------------------- geocercas
_URBAN_PERIMETER = Polygon(
    [
        (-74.21, 4.60),
        (-74.15, 4.53),
        (-74.08, 4.53),
        (-74.06, 4.60),
        (-74.02, 4.68),
        (-74.02, 4.82),
        (-74.07, 4.83),
        (-74.11, 4.75),
        (-74.17, 4.70),
        (-74.21, 4.64),
    ]
)

DEPOT_LONLAT = (-74.1500, 4.6680)
SCHOOL_LONLAT = (-74.0850, 4.6370)
CALLE80_LONLAT = (-74.0580, 4.7000)


def geofences() -> list[Geofence]:
    """Geocercas con IDs estables (se insertan en este orden en ``ref.geofence``)."""
    return [
        Geofence(1, "BOG-URB", "Perímetro urbano de Bogotá", ZoneType.URBAN, 50, _URBAN_PERIMETER),
        Geofence(
            2,
            "BOG-AUTONORTE",
            "Corredor Autopista Norte",
            ZoneType.HIGHWAY,
            50,
            corridor(_R01[5:], half_width_m=120),
        ),
        Geofence(
            3,
            "BOG-CL80-AUTONORTE",
            "Intersección Calle 80 - Autopista Norte (alta siniestralidad)",
            ZoneType.HIGH_RISK,
            50,
            circle(*CALLE80_LONLAT, radius_m=350),
        ),
        Geofence(
            4,
            "BOG-AUTOSUR",
            "Corredor Autopista Sur (alta siniestralidad)",
            ZoneType.HIGH_RISK,
            50,
            corridor(_R02[3:6], half_width_m=150),
        ),
        Geofence(
            5,
            "BOG-ESC-TEUSAQUILLO",
            "Zona escolar Teusaquillo",
            ZoneType.SCHOOL,
            30,
            circle(*SCHOOL_LONLAT, radius_m=300),
        ),
        Geofence(
            6,
            "BOG-PATIO-FONTIBON",
            "Patio logístico Fontibón",
            ZoneType.DEPOT,
            20,
            circle(*DEPOT_LONLAT, radius_m=250),
        ),
    ]


def routes() -> list[PlannedRoute]:
    return [
        PlannedRoute(1, "R01", "Fontibón → Autopista Norte (Calle 170)", LineString(_R01)),
        PlannedRoute(2, "R02", "Fontibón → Autopista Sur → Soacha", LineString(_R02)),
        PlannedRoute(3, "R03", "Fontibón → Teusaquillo → Centro", LineString(_R03)),
    ]


def vehicles() -> list[Vehicle]:
    return [
        Vehicle("VHC-8472-ABC", fuel_tank_capacity_l=200, nominal_consumption_l_100km=32),
        Vehicle("VHC-1935-BOG", fuel_tank_capacity_l=200, nominal_consumption_l_100km=35),
        Vehicle("VHC-5521-KLM", fuel_tank_capacity_l=120, nominal_consumption_l_100km=22),
        Vehicle("VHC-7310-TRZ", fuel_tank_capacity_l=150, nominal_consumption_l_100km=38),
        Vehicle("VHC-2048-QWE", fuel_tank_capacity_l=200, nominal_consumption_l_100km=30),
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
    ]
