"""Inicio por defecto del simulador: nunca una jornada que aún no terminó."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from simon_geo.simulator.__main__ import _default_start


@pytest.mark.parametrize(
    ("now_utc", "expected_utc"),
    [
        # 00:19 en Bogotá: la jornada de hoy (06:00) no ha empezado → ayer
        (datetime(2026, 11, 21, 5, 19, tzinfo=UTC), datetime(2026, 11, 20, 11, 0, tzinfo=UTC)),
        # 06:30 en Bogotá: la de hoy sigue en curso → ayer
        (datetime(2026, 11, 21, 11, 30, tzinfo=UTC), datetime(2026, 11, 20, 11, 0, tzinfo=UTC)),
        # 15:00 en Bogotá: la de hoy ya terminó → hoy
        (datetime(2026, 11, 21, 20, 0, tzinfo=UTC), datetime(2026, 11, 21, 11, 0, tzinfo=UTC)),
    ],
)
def test_default_start_is_last_finished_shift(now_utc: datetime, expected_utc: datetime) -> None:
    assert _default_start(now_utc) == expected_utc
