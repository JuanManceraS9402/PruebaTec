"""Reglas de aceptación de paquetes antes de persistir en Bronce."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from simon_geo.api.repository import TelemetryWriter
from simon_geo.api.schemas import IngestResponse, RejectedPacket, TelemetryIn


class IngestService:
    """Valida la ventana temporal por paquete y persiste el resto del lote.

    Un reloj de equipo desfasado no debe tumbar el lote completo de la flota:
    los paquetes fuera de ventana se rechazan individualmente y se informan.
    """

    def __init__(
        self,
        writer: TelemetryWriter,
        *,
        max_future: timedelta,
        max_age: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        source: str = "api",
    ) -> None:
        self._writer = writer
        self._max_future = max_future
        self._max_age = max_age
        self._clock = clock
        self._source = source

    def ingest(self, packets: Sequence[TelemetryIn]) -> IngestResponse:
        now = self._clock()
        accepted = []
        rejected: list[RejectedPacket] = []
        for i, packet in enumerate(packets):
            reason = self._time_window_violation(packet.event_ts, now)
            if reason:
                rejected.append(
                    RejectedPacket(
                        index=i,
                        vehicle_id=packet.vehicle_id,
                        event_ts=packet.event_ts,
                        reason=reason,
                    )
                )
            else:
                accepted.append(packet.to_domain())

        inserted = self._writer.insert_raw(accepted, self._source) if accepted else 0
        return IngestResponse(
            received=len(packets),
            inserted=inserted,
            duplicates=len(accepted) - inserted,
            rejected=rejected,
        )

    def _time_window_violation(self, event_ts: datetime, now: datetime) -> str | None:
        if event_ts > now + self._max_future:
            return "event_ts en el futuro: reloj del equipo desfasado"
        if event_ts < now - self._max_age:
            return f"event_ts con más de {self._max_age.days} días de antigüedad"
        return None
