"""Autenticación por API key y mapeo a roles de base de datos.

La API no enmascara datos por su cuenta: según la key, la consulta se ejecuta
con el usuario de BD del analista o del administrador, y son las vistas de
``analytics`` las que deciden qué se ve (una sola fuente de verdad).
"""

from __future__ import annotations

import secrets
from enum import StrEnum

from simon_geo.config import DbRole, Settings


class ApiRole(StrEnum):
    INGEST = "ingest"
    ANALYST = "analyst"
    ADMIN = "admin"

    @property
    def db_role(self) -> DbRole:
        return {
            ApiRole.INGEST: DbRole.INGEST,
            ApiRole.ANALYST: DbRole.ANALYST,
            ApiRole.ADMIN: DbRole.ADMIN,
        }[self]


def resolve_role(api_key: str | None, settings: Settings) -> ApiRole | None:
    if not api_key:
        return None
    keys = {
        ApiRole.INGEST: settings.api_key_ingest,
        ApiRole.ANALYST: settings.api_key_analyst,
        ApiRole.ADMIN: settings.api_key_admin,
    }
    for role, expected in keys.items():
        # comparación en tiempo constante (evita ataques de temporización)
        if secrets.compare_digest(api_key.encode(), expected.get_secret_value().encode()):
            return role
    return None
