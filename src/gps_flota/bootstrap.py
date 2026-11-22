"""Inicializa la base en un PostgreSQL + PostGIS instalado sin Docker.

python -m gps_flota.bootstrap            # crea la base y ejecuta db/init/*.sql
python -m gps_flota.bootstrap --reset    # borra la base y los roles del proyecto y empieza de cero

Replica lo que hace el contenedor ``postgis/postgis`` al crear el volumen:
crea la base ``POSTGRES_DB``, ejecuta en orden los ``db/init/*.sql`` y crea los
usuarios de login (lo que en Docker hace ``07_users.sh``). Usa las mismas
variables del ``.env``, así que el resto del proyecto no cambia.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from gps_flota.config import DbRole, Settings, get_settings

INIT_DIR: Final = Path(__file__).resolve().parents[2] / "db" / "init"

# Roles que crean 06_security.sql (grupo) y _create_login_users (login).
GROUP_ROLES: Final = ("telemetry_ingest", "pipeline_worker", "bi_analyst", "fleet_admin")


def _login_users(s: Settings) -> list[tuple[str, str, str, str | None]]:
    """(usuario, contraseña, rol de grupo, statement_timeout)."""
    return [
        (s.app_ingest_user, s.app_ingest_password.get_secret_value(), "telemetry_ingest", None),
        (s.app_etl_user, s.app_etl_password.get_secret_value(), "pipeline_worker", None),
        (s.app_analyst_user, s.app_analyst_password.get_secret_value(), "bi_analyst", "30s"),
        (s.app_admin_user, s.app_admin_password.get_secret_value(), "fleet_admin", "60s"),
    ]


def _server_conninfo(s: Settings) -> str:
    """Conexión de superusuario a la base de mantenimiento ``postgres``."""
    return make_conninfo(
        s.conninfo(DbRole.OWNER), dbname="postgres", application_name="gps_flota_bootstrap"
    )


def reset(s: Settings) -> None:
    with psycopg.connect(_server_conninfo(s), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(s.postgres_db))
        )
        for user, *_ in _login_users(s):
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(user)))
        for role in GROUP_ROLES:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    print(f"Base {s.postgres_db!r} y roles del proyecto eliminados.")


def create_database(s: Settings) -> None:
    with psycopg.connect(_server_conninfo(s), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (s.postgres_db,)
        ).fetchone()
        if exists:
            raise SystemExit(
                f"La base {s.postgres_db!r} ya existe. Usa --reset para recrearla desde cero."
            )
        clashing = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(GROUP_ROLES),)
        ).fetchall()
        if clashing:
            names = ", ".join(r[0] for r in clashing)
            raise SystemExit(f"Ya existen roles del proyecto ({names}). Usa --reset.")
        available = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'postgis'"
        ).fetchone()
        if not available:
            raise SystemExit(
                "PostGIS no está instalado en este servidor. Instálalo con Stack Builder "
                "(Spatial Extensions → PostGIS) y vuelve a intentarlo."
            )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(s.postgres_db)))


def run_init_scripts(s: Settings) -> None:
    scripts = sorted(INIT_DIR.glob("*.sql"))
    if not scripts:
        raise SystemExit(f"No hay scripts en {INIT_DIR}")
    with psycopg.connect(s.conninfo(DbRole.OWNER), autocommit=True) as conn:
        for script in scripts:
            # Sin parámetros, psycopg envía el archivo tal cual (varias sentencias).
            conn.execute(script.read_text(encoding="utf-8"))
            print(f"  OK {script.name}")
        _create_login_users(conn, s)
        print("  OK usuarios de login (equivalente a 07_users.sh)")


def _create_login_users(conn: psycopg.Connection, s: Settings) -> None:
    for user, password, group, timeout in _login_users(s):
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                sql.Identifier(user), sql.Literal(password), sql.Identifier(group)
            )
        )
        if timeout:
            conn.execute(
                sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                    sql.Identifier(user), sql.Literal(timeout)
                )
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inicializa la base sin Docker")
    parser.add_argument("--reset", action="store_true", help="borrar base y roles antes de crear")
    args = parser.parse_args(argv)
    s = get_settings()
    try:
        if args.reset:
            reset(s)
        create_database(s)
        print(f"Base {s.postgres_db!r} creada en {s.db_host}:{s.db_port}. Ejecutando db/init:")
        run_init_scripts(s)
    except psycopg.OperationalError as exc:
        print(f"No se pudo conectar a PostgreSQL: {exc}", file=sys.stderr)
        return 2
    print("Listo. Siguiente paso: python -m gps_flota.seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
