"""Configuración centralizada, leída de variables de entorno / archivo .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DbRole(StrEnum):
    """Usuarios de base de datos; cada componente se conecta con el mínimo privilegio."""

    OWNER = "owner"  # dueño del esquema: solo para cargar datos maestros (seed)
    INGEST = "ingest"  # API de ingesta → solo INSERT en Bronce
    ETL = "etl"  # worker Bronce → Plata → Oro
    ANALYST = "analyst"  # lectura enmascarada (analytics.*)
    ADMIN = "admin"  # lectura sin enmascarar


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Base de datos ---
    db_host: str = "localhost"
    db_port: int = 5432
    postgres_db: str = "simon_geo"

    postgres_user: str = "postgres"
    postgres_password: SecretStr = SecretStr("postgres_dev")
    app_ingest_user: str = "api_ingest"
    app_ingest_password: SecretStr = SecretStr("ingest_dev")
    app_etl_user: str = "etl_worker"
    app_etl_password: SecretStr = SecretStr("etl_dev")
    app_analyst_user: str = "analyst_demo"
    app_analyst_password: SecretStr = SecretStr("analyst_dev")
    app_admin_user: str = "admin_demo"
    app_admin_password: SecretStr = SecretStr("admin_dev")

    # --- API ---
    api_key_ingest: SecretStr = SecretStr("dev-ingest-key")
    api_key_analyst: SecretStr = SecretStr("dev-analyst-key")
    api_key_admin: SecretStr = SecretStr("dev-admin-key")
    ingest_max_batch: int = 5000
    ingest_max_future_s: int = 300  # tolerancia a relojes de equipo adelantados
    ingest_max_age_days: int = 30  # paquetes más viejos se rechazan (buffer de equipo)

    def _credentials(self, role: DbRole) -> tuple[str, SecretStr]:
        return {
            DbRole.OWNER: (self.postgres_user, self.postgres_password),
            DbRole.INGEST: (self.app_ingest_user, self.app_ingest_password),
            DbRole.ETL: (self.app_etl_user, self.app_etl_password),
            DbRole.ANALYST: (self.app_analyst_user, self.app_analyst_password),
            DbRole.ADMIN: (self.app_admin_user, self.app_admin_password),
        }[role]

    def conninfo(self, role: DbRole) -> str:
        """Cadena de conexión libpq para el rol indicado (con escape de valores)."""
        user, password = self._credentials(role)
        return make_conninfo(
            host=self.db_host,
            port=self.db_port,
            dbname=self.postgres_db,
            user=user,
            password=password.get_secret_value(),
            application_name=f"simon_geo_{role.value}",
            connect_timeout=5,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
