"""Aplicación FastAPI.

uvicorn simon_geo.api.app:app --reload
Documentación interactiva: http://localhost:8000/docs
"""

# Sin `from __future__ import annotations`: FastAPI necesita evaluar las anotaciones
# de los endpoints, y las dependencias se crean dentro de create_app (closures).
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from simon_geo import __version__
from simon_geo.api.repository import AlertReader, PostgresRepository, TelemetryWriter
from simon_geo.api.schemas import AlertOut, IngestRequest, IngestResponse
from simon_geo.api.security import ApiRole, resolve_role
from simon_geo.api.service import IngestService
from simon_geo.config import Settings, get_settings
from simon_geo.db import PoolRegistry


def create_app(
    *,
    settings: Settings | None = None,
    writer: TelemetryWriter | None = None,
    reader: AlertReader | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Fábrica de la app. Los tests inyectan repositorios en memoria."""
    settings = settings or get_settings()
    pools: PoolRegistry | None = None
    if writer is None or reader is None:
        pools = PoolRegistry(settings)
        pg = PostgresRepository(pools)
        writer = writer or pg
        reader = reader or pg

    service = IngestService(
        writer,
        max_future=timedelta(seconds=settings.ingest_max_future_s),
        max_age=timedelta(days=settings.ingest_max_age_days),
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if pools is not None:
            pools.close()

    app = FastAPI(
        title="Simón Geo · Telemetría AVL",
        version=__version__,
        description="Ingesta de telemetría vehicular (capa Bronce) y consulta de alertas.",
        lifespan=lifespan,
    )

    def require(*allowed: ApiRole) -> Callable[..., ApiRole]:
        def dependency(x_api_key: Annotated[str | None, Header()] = None) -> ApiRole:
            role = resolve_role(x_api_key, settings)
            if role is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key ausente o inválida")
            if role not in allowed:
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"El rol {role} no tiene acceso")
            return role

        return dependency

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post(
        "/v1/telemetry",
        response_model=IngestResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["ingesta"],
        summary="Ingesta por lotes de paquetes AVL (idempotente)",
    )
    def ingest(
        body: IngestRequest,
        _: Annotated[ApiRole, Depends(require(ApiRole.INGEST, ApiRole.ADMIN))],
    ) -> IngestResponse:
        if len(body.packets) > settings.ingest_max_batch:
            raise HTTPException(
                413,  # Content Too Large (el nombre de la constante cambia entre versiones)
                f"Máximo {settings.ingest_max_batch} paquetes por lote",
            )
        return service.ingest(body.packets)

    @app.get(
        "/v1/alerts",
        response_model=list[AlertOut],
        tags=["consulta"],
        summary="Alertas recientes (IDs enmascarados para analistas)",
    )
    def alerts(
        role: Annotated[ApiRole, Depends(require(ApiRole.ANALYST, ApiRole.ADMIN))],
        since: Annotated[datetime | None, Query(description="Por defecto, últimas 24 h")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[AlertOut]:
        since = since or clock() - timedelta(hours=24)
        rows = reader.recent_alerts(role.db_role, since, limit)
        return [AlertOut(**row) for row in rows]

    return app


app = create_app()
