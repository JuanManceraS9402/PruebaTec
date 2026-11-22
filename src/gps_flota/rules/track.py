"""Procesamiento secuencial de la traza de un vehículo (núcleo de la capa Plata).

Combina cinemática, geocercas, velocidad y combustible punto a punto, manteniendo
el estado necesario entre reportes:

* último punto válido (para no encadenar outliers),
* odómetro acumulado y ventana de consumo,
* episodio de exceso / riesgo de combustible en curso (para no repetir alertas).

Política de alertas: una alerta al INICIAR un episodio y otra solo si la
severidad ESCALA dentro del mismo episodio. Así 3 minutos a 70 km/h en zona de
50 generan una alerta, no 60.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from gps_flota.domain import Alert, AlertType, Geofence, Severity, TelemetryPoint, Vehicle
from gps_flota.parameters import PipelineParameters
from gps_flota.rules.fuel import (
    ConsumptionEstimator,
    FuelRiskAssessment,
    assess_fuel_risk,
    estimate_range_km,
    is_refuel,
)
from gps_flota.rules.kinematics import segment_metrics
from gps_flota.rules.speed import SpeedEvaluation, evaluate_speed
from gps_flota.spatial.geofencing import GeofenceIndex, SpeedLimitDecision, resolve_speed_limit
from gps_flota.spatial.routes import PlannedRoute

# Si llegan N "outliers" seguidos, lo más probable es que el outlier fuera el
# punto de referencia (p.ej. el primer punto del día): se re-ancla la traza.
MAX_CONSECUTIVE_OUTLIERS: Final = 3


@dataclass(frozen=True, slots=True)
class EnrichedPoint:
    """Fila de ``silver.telemetry_enriched``."""

    vehicle_id: str
    event_ts: datetime
    lon: float
    lat: float
    speed_kmh: float
    fuel_level_pct: float
    prev_event_ts: datetime | None
    delta_s: float | None
    segment_distance_m: float
    implied_speed_kmh: float | None
    accel_mps2: float | None
    fuel_delta_pct: float | None
    is_refuel: bool
    is_gps_outlier: bool
    is_harsh_event: bool
    geofence_ids: tuple[int, ...]
    applied_speed_limit_kmh: float
    speed_excess_kmh: float
    is_speeding: bool
    consumption_l_100km: float
    est_range_km: float
    remaining_route_km: float | None
    is_low_fuel_risk: bool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    point: EnrichedPoint
    alerts: tuple[Alert, ...]


class TrackProcessor:
    def __init__(
        self,
        vehicle: Vehicle,
        geofence_index: GeofenceIndex,
        params: PipelineParameters,
        route: PlannedRoute | None = None,
    ) -> None:
        self._vehicle = vehicle
        self._index = geofence_index
        self._params = params
        self._route = route
        self._estimator = ConsumptionEstimator(
            tank_capacity_l=vehicle.fuel_tank_capacity_l,
            nominal_l_100km=vehicle.nominal_consumption_l_100km,
            window_km=params.consumption_window_km,
            min_sample_km=params.min_consumption_sample_km,
        )
        self._last_point: TelemetryPoint | None = None
        self._last_valid: TelemetryPoint | None = None
        self._last_valid_geofences: list[Geofence] = []
        self._consecutive_outliers = 0
        self._odometer_km = 0.0
        self._speeding_severity: Severity | None = None
        self._fuel_severity: Severity | None = None
        self._fuel_clear_from_km: float | None = None

    @property
    def odometer_km(self) -> float:
        return self._odometer_km

    def process(
        self, point: TelemetryPoint, geofence_ids: Sequence[int] | None = None
    ) -> ProcessResult:
        """Procesa el siguiente punto de la traza.

        ``geofence_ids``: geocercas del punto ya resueltas en PostGIS (modo batch,
        ``ST_Intersects`` con índice GiST). Si es ``None`` se resuelven con el
        índice en memoria (modo tiempo real).
        """
        if point.vehicle_id != self._vehicle.vehicle_id:
            raise ValueError(
                f"Punto de {point.vehicle_id} enviado al procesador de {self._vehicle.vehicle_id}"
            )
        if self._last_point is not None and point.event_ts <= self._last_point.event_ts:
            raise ValueError(
                f"Puntos fuera de orden o duplicados para {point.vehicle_id} en "
                f"{point.event_ts.isoformat()}: deduplicar y ordenar antes de procesar"
            )

        # 1. Cinemática respecto al último punto válido
        prev_valid = self._last_valid
        seg = segment_metrics(prev_valid, point, self._params)
        is_outlier = seg.is_gps_outlier
        if is_outlier:
            self._consecutive_outliers += 1
            if self._consecutive_outliers >= MAX_CONSECUTIVE_OUTLIERS:
                is_outlier = False  # re-anclaje: este punto pasa a ser la referencia
        if not is_outlier:
            self._consecutive_outliers = 0

        # 2. Combustible: tanqueos y delta respecto al reporte anterior (sea o no outlier:
        #    el sensor de nivel es independiente del GPS)
        prev_any = self._last_point
        fuel_delta = None if prev_any is None else point.fuel_level_pct - prev_any.fuel_level_pct
        refuel = prev_any is not None and is_refuel(
            prev_any.fuel_level_pct, point.fuel_level_pct, self._params.refuel_min_jump_pct
        )

        # 3. Geocercas: si la posición es un outlier se usan las de la última posición válida
        if is_outlier and prev_valid is not None:
            geofences = self._last_valid_geofences
        elif geofence_ids is not None:
            geofences = self._index.by_ids(geofence_ids)
        else:
            geofences = self._index.containing(point.lon, point.lat)
        decision = resolve_speed_limit(geofences, self._params.default_speed_limit_kmh)
        speed_eval = evaluate_speed(point.speed_kmh, decision, self._params.speed_tolerance_kmh)

        # 4. Consumo, autonomía y riesgo de ruta
        if refuel:
            self._estimator.reset()
            self._fuel_severity = None  # tanquear cierra el episodio de combustible
            self._fuel_clear_from_km = None
        if not is_outlier:
            # un re-anclaje no suma el "salto" al odómetro
            if not seg.is_gps_outlier:
                self._odometer_km += seg.distance_m / 1000
            self._estimator.add(self._odometer_km, point.fuel_level_pct)
        consumption = self._estimator.estimate()
        range_km = estimate_range_km(
            point.fuel_level_pct, self._vehicle.fuel_tank_capacity_l, consumption.l_100km
        )
        risk: FuelRiskAssessment | None = None
        if self._route is not None and not is_outlier:
            remaining = self._route.remaining_km(point.lon, point.lat)
            risk = assess_fuel_risk(range_km, remaining, self._params.fuel_safety_factor)

        # 5. Alertas
        alerts: list[Alert] = []
        speeding_alert = self._speeding_alert(point, speed_eval, decision)
        if speeding_alert:
            alerts.append(speeding_alert)
        fuel_alert = self._fuel_alert(point, risk, consumption.l_100km, consumption.source)
        if fuel_alert:
            alerts.append(fuel_alert)

        # 6. Estado
        self._last_point = point
        if not is_outlier:
            self._last_valid = point
            self._last_valid_geofences = geofences

        enriched = EnrichedPoint(
            vehicle_id=point.vehicle_id,
            event_ts=point.event_ts,
            lon=point.lon,
            lat=point.lat,
            speed_kmh=point.speed_kmh,
            fuel_level_pct=point.fuel_level_pct,
            prev_event_ts=prev_valid.event_ts if prev_valid else None,
            delta_s=seg.delta_s,
            segment_distance_m=seg.distance_m,
            implied_speed_kmh=seg.implied_speed_kmh,
            accel_mps2=seg.accel_mps2,
            fuel_delta_pct=fuel_delta,
            is_refuel=refuel,
            is_gps_outlier=is_outlier,
            is_harsh_event=seg.is_harsh_event,
            geofence_ids=tuple(g.geofence_id for g in geofences),
            applied_speed_limit_kmh=speed_eval.limit_kmh,
            speed_excess_kmh=speed_eval.excess_kmh,
            is_speeding=speed_eval.is_speeding,
            consumption_l_100km=consumption.l_100km,
            est_range_km=range_km,
            remaining_route_km=risk.remaining_km if risk else None,
            is_low_fuel_risk=bool(risk and risk.is_risk),
        )
        return ProcessResult(point=enriched, alerts=tuple(alerts))

    # ------------------------------------------------------------------ alertas
    def _speeding_alert(
        self, point: TelemetryPoint, ev: SpeedEvaluation, decision: SpeedLimitDecision
    ) -> Alert | None:
        if not ev.is_speeding or ev.severity is None:
            self._speeding_severity = None  # fin del episodio
            return None
        if self._speeding_severity is not None and ev.severity.rank <= self._speeding_severity.rank:
            return None  # mismo episodio, sin escalar
        self._speeding_severity = ev.severity
        governing = decision.governing
        return Alert(
            vehicle_id=point.vehicle_id,
            event_ts=point.event_ts,
            alert_type=AlertType.SPEEDING,
            severity=ev.severity,
            lon=point.lon,
            lat=point.lat,
            observed_value=point.speed_kmh,
            threshold_value=ev.limit_kmh,
            geofence_id=governing.geofence_id if governing else None,
            details={
                "excess_kmh": round(ev.excess_kmh, 1),
                "zone_type": governing.zone_type.value if governing else None,
                "geofence_code": governing.code if governing else None,
            },
        )

    def _update_fuel_recovery(self, risk: FuelRiskAssessment) -> None:
        """Cierra el episodio solo con una recuperación sostenida.

        El ruido del sensor mueve la autonomía ~0,7 km en un tanque de 150 L: cerca del fin
        de la ruta eso cruza cualquier margen relativo. Por eso se exige mantenerse sobre el
        margen durante ``fuel_alert_clear_km`` seguidos.
        """
        if risk.range_km < risk.required_km * self._params.fuel_alert_clear_ratio:
            self._fuel_clear_from_km = None
            return
        if self._fuel_clear_from_km is None:
            self._fuel_clear_from_km = self._odometer_km
        if self._odometer_km - self._fuel_clear_from_km >= self._params.fuel_alert_clear_km:
            self._fuel_severity = None
            self._fuel_clear_from_km = None

    def _fuel_alert(
        self,
        point: TelemetryPoint,
        risk: FuelRiskAssessment | None,
        consumption_l_100km: float,
        consumption_source: str,
    ) -> Alert | None:
        if risk is None:
            return None  # sin ruta o posición no fiable: se mantiene el estado
        if not risk.is_risk or risk.severity is None:
            self._update_fuel_recovery(risk)
            return None
        self._fuel_clear_from_km = None
        if self._fuel_severity is not None and risk.severity.rank <= self._fuel_severity.rank:
            return None
        self._fuel_severity = risk.severity
        return Alert(
            vehicle_id=point.vehicle_id,
            event_ts=point.event_ts,
            alert_type=AlertType.LOW_FUEL_RANGE,
            severity=risk.severity,
            lon=point.lon,
            lat=point.lat,
            observed_value=round(risk.range_km, 2),
            threshold_value=round(risk.required_km, 2),
            details={
                "remaining_route_km": round(risk.remaining_km, 2),
                "fuel_level_pct": point.fuel_level_pct,
                "consumption_l_100km": round(consumption_l_100km, 2),
                "consumption_source": consumption_source,
            },
        )
