"""Schemi degli insight (`public.insights`) e dei relativi filtri di ricerca."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.analysis import AnalysisMode

InsightFilterMode = Literal["INFO", "STYLE", "BOTH", "SCRIPT"]
"""Filtro di `GET /api/v1/insights`.

`INFO`/`STYLE`/`BOTH` filtrano su `analysis_mode`. `SCRIPT` è un filtro diverso
in natura: seleziona i record che *hanno* uno script inverso
(`inverse_script_template IS NOT NULL`), a prescindere dalla modalità con cui
sono stati generati.
"""


class InsightResponse(BaseModel):
    """Un record di `insights` così come viene restituito al client.

    I tre payload restano `dict` e non modelli tipizzati **in lettura**: le
    colonne sono `jsonb` e contengono lo schema in vigore al momento
    dell'analisi. Tipizzarli qui significherebbe far fallire la lettura di tutto
    lo storico a ogni evoluzione di `VideoAnalysisResponse`. La validazione
    forte avviene dove serve davvero, cioè *prima* della scrittura
    (`app.services.gemini_service`).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    creator_id: UUID | None
    user_id: UUID
    video_url: str
    thumbnail_url: str | None
    analysis_mode: AnalysisMode
    summary_data: dict[str, Any] | None = Field(
        default=None, description="Payload INFO prodotto dall'analisi."
    )
    style_data: dict[str, Any] | None = Field(
        default=None, description="Payload STYLE prodotto dall'analisi."
    )
    inverse_script_template: dict[str, Any] | None = Field(
        default=None, description="Script inverso riutilizzabile, se generato."
    )
    keywords: list[str] = Field(
        default_factory=list, description="Keyword estratte, usate per la ricerca."
    )
    created_at: datetime


class InsightListResponse(BaseModel):
    """Pagina di risultati di `GET /api/v1/insights`."""

    items: list[InsightResponse] = Field(description="Insight della pagina corrente.")
    page: int = Field(description="Pagina richiesta, 1-based.")
    limit: int = Field(description="Dimensione della pagina.")
    total: int = Field(description="Totale dei record che soddisfano i filtri.")
    has_more: bool = Field(description="True se esiste almeno una pagina successiva.")


class CronCreatorResult(BaseModel):
    """Esito del controllo di un singolo creator nel job cron."""

    creator_id: UUID
    username: str
    platform: str
    status: Literal["ok", "failed", "skipped"] = Field(
        description="'failed' quando Apify fallisce per questo creator; il job prosegue comunque."
    )
    videos_found: int = Field(default=0, description="Video restituiti dallo scraper.")
    new_videos: int = Field(default=0, description="Video non ancora presenti in insights.")
    queued: int = Field(default=0, description="Analisi effettivamente accodate in background.")
    error: str | None = Field(
        default=None,
        description="Motivo sintetico del fallimento. Nessuno stack trace, nessuna credenziale.",
    )


class CronRunResponse(BaseModel):
    """Riepilogo di `POST /api/v1/cron/check-updates`."""

    checked_creators: int = Field(description="Creator attivi presi in carico.")
    failed_creators: int = Field(description="Creator per cui lo scraping è fallito.")
    queued_analyses: int = Field(description="Analisi accodate in background.")
    results: list[CronCreatorResult] = Field(description="Dettaglio per creator.")
    skipped: bool = Field(
        default=False,
        description=(
            "`true` se il giro è stato saltato perché un'altra esecuzione era già "
            "in corso. Non è un errore: la risposta resta 200 perché lo scheduler "
            "ha fatto il suo lavoro correttamente, e ritentare peggiorerebbe le "
            "cose. Gli altri contatori sono a zero."
        ),
    )
