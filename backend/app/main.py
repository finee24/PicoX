"""Entrypoint dell'API Picox."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.api.v1 import analyze, creators, cron, insights
from app.core.config import get_settings
from app.middleware.error_handler import register_exception_handlers
from app.services.supabase_service import close_service_client

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )
    # httpx logga ogni richiesta con l'URL completo: verso Supabase e Gemini
    # quell'URL può contenere parametri che non vogliamo nei log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)
    logger.info(
        "Picox API avviata (environment=%s, modello=%s)",
        settings.environment,
        settings.gemini_model,
    )
    try:
        yield
    finally:
        await close_service_client()
        logger.info("Picox API arrestata.")


class HealthResponse(BaseModel):
    """Risposta di `GET /health`."""

    status: Literal["ok"] = Field(description="Stato del servizio.")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Picox API",
        description=(
            "Analisi AI multimodale di video brevi: sintesi dei contenuti, analisi "
            "dello stile e script inverso riutilizzabile."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # CORS limitato al frontend configurato. Nessun wildcard: `allow_credentials`
    # con `allow_origins=["*"]` è la combinazione che espone l'API a qualunque
    # sito visitato dall'utente.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    register_exception_handlers(app)

    app.include_router(analyze.router)
    app.include_router(creators.router)
    app.include_router(insights.router)
    app.include_router(cron.router)

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["system"],
        summary="Health check",
    )
    async def health() -> HealthResponse:
        """Liveness probe per Render/Railway. Non autenticata, non tocca il database."""
        return HealthResponse(status="ok")

    return app


app = create_app()
