"""`POST /api/v1/analyze-video` — analisi di un singolo video.

Pipeline:

1. normalizzazione dell'URL (chiave della cache e del vincolo di unicità);
2. **cache check** su `(user_id, video_url)` con il client scoped al JWT: se il
   video è già stato analizzato si restituisce il record esistente senza toccare
   né Apify né Gemini;
3. risoluzione best effort dei metadati via Apify (URL diretto del media,
   thumbnail, durata);
4. download in un file temporaneo, con limiti di dimensione e durata e cleanup
   garantito;
5. inferenza Gemini in structured output;
6. scrittura su `insights` con il client service-role.

`perform_analysis` è riusata dal job cron, che esegue gli stessi passi in
background senza un JWT di sessione.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.middleware.error_handler import SafeRoute
from app.schemas.analysis import AnalysisMode, AnalyzeVideoRequest, VideoAnalysisResponse
from app.schemas.insights import InsightResponse
from app.services.apify_service import ApifyService, ScrapedVideo, get_apify_service
from app.services.gemini_service import GeminiService, get_gemini_service
from app.services.media_service import download_to_temp, normalize_video_url
from app.services.supabase_service import db_errors, scoped_client, service_table

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["analysis"], route_class=SafeRoute)


# =============================================================================
# Lettura della cache
# =============================================================================


# Colonne che ciascuna modalità si impegna a valorizzare. Servono a decidere se
# una riga già in archivio *copre* la richiesta: `UNIQUE (user_id, video_url)`
# ammette una sola riga per video, quindi senza questo controllo un'analisi
# `INFO` fatta ieri soddisfaceva anche una richiesta `BOTH` di oggi, restituendo
# 200 con `style_data` e `inverse_script_template` a `null`.
_COLONNE_PER_MODALITA: Final[dict[str, tuple[str, ...]]] = {
    "INFO": ("summary_data",),
    "STYLE": ("style_data",),
    "BOTH": ("summary_data", "style_data", "inverse_script_template"),
}


def _copre_la_modalita(record: InsightResponse, mode: AnalysisMode) -> bool:
    """`True` se la riga contiene già tutto ciò che `mode` promette.

    Il confronto è per *copertura*, non per uguaglianza: una riga `BOTH`
    soddisfa una richiesta `INFO`, e restituirla è corretto oltre che gratuito.
    L'uguaglianza secca sarebbe peggio del bug che corregge — una richiesta
    `INFO` su una riga `BOTH` verrebbe considerata cache miss, e l'upsert
    successivo sovrascriverebbe la riga degradandola, buttando via lo stile e lo
    script inverso già pagati.
    """
    return all(
        getattr(record, colonna) is not None
        for colonna in _COLONNE_PER_MODALITA[mode]
    )


async def find_cached_insight(
    user_id: str,
    video_url: str,
    *,
    access_token: str | None,
    settings: Settings,
    required_mode: AnalysisMode | None = None,
) -> InsightResponse | None:
    """Cerca un insight già presente per `(user_id, video_url)`.

    Con un JWT disponibile si usa il client scoped, così il RLS resta la rete di
    sicurezza sulla lettura. Il job cron non ha una sessione utente e ricade sul
    client service-role, che qui è comunque filtrato su `user_id`.

    Con `required_mode` la riga vale come cache hit solo se copre quella
    modalità; altrimenti si restituisce `None` e il chiamante rianalizza. Senza,
    la riga viene restituita così com'è — è il caso della rilettura dopo
    l'upsert, dove il record è per definizione quello appena scritto.
    """
    async with db_errors("cache lookup insights"):
        if access_token is not None:
            async with scoped_client(access_token, settings) as db:
                result = await (
                    db.table("insights")
                    .select("*")
                    .eq("user_id", user_id)
                    .eq("video_url", video_url)
                    .limit(1)
                    .execute()
                )
        else:
            insights = await service_table("insights", user_id)
            result = await (
                insights.select("*").eq("video_url", video_url).limit(1).execute()
            )

    if not result.data:
        return None

    record = InsightResponse.model_validate(result.data[0])
    if required_mode is not None and not _copre_la_modalita(record, required_mode):
        logger.info(
            "Riga in cache insufficiente per la modalità %s (archiviata: %s): si rianalizza.",
            required_mode,
            record.analysis_mode,
        )
        return None
    return record


# =============================================================================
# Pipeline
# =============================================================================


def _build_insight_payload(
    analysis: VideoAnalysisResponse,
    *,
    video_url: str,
    mode: AnalysisMode,
    creator_id: UUID | str | None,
    thumbnail_url: str | None,
) -> dict[str, Any]:
    """Traduce l'analisi validata nelle colonne di `insights`.

    I payload sono serializzati con `mode="json"` e **senza** `exclude_none`: la
    forma del `jsonb` resta stabile fra un record e l'altro, così il frontend
    distingue "campo assente perché il video non ce l'aveva" da "campo che non
    esisteva nello schema di allora".
    """
    return {
        "creator_id": str(creator_id) if creator_id else None,
        "video_url": video_url,
        "thumbnail_url": thumbnail_url,
        "analysis_mode": mode,
        "summary_data": (
            analysis.info_analysis.model_dump(mode="json")
            if analysis.info_analysis is not None
            else None
        ),
        "style_data": (
            analysis.style_analysis.model_dump(mode="json")
            if analysis.style_analysis is not None
            else None
        ),
        "inverse_script_template": (
            analysis.inverse_script.model_dump(mode="json")
            if analysis.inverse_script is not None
            else None
        ),
        "keywords": list(analysis.keywords),
    }


async def perform_analysis(
    *,
    user_id: str,
    video_url: str,
    mode: AnalysisMode,
    settings: Settings,
    gemini: GeminiService,
    apify: ApifyService,
    access_token: str | None = None,
    creator_id: UUID | str | None = None,
    scraped: ScrapedVideo | None = None,
) -> tuple[InsightResponse, bool]:
    """Analizza un video e ne persiste il risultato.

    Restituisce `(record, from_cache)`. `scraped` permette al job cron di
    riutilizzare i metadati già ottenuti dallo scraping, evitando una seconda
    chiamata ad Apify per lo stesso video.
    """
    cached = await find_cached_insight(
        user_id,
        video_url,
        access_token=access_token,
        settings=settings,
        required_mode=mode,
    )
    if cached is not None:
        logger.info("Cache hit su insights per l'utente %s", user_id)
        return cached, True

    # Best effort: serve l'URL diretto del media, perché la pagina del post non
    # è un file video. Un fallimento non è fatale — si tenta comunque l'URL
    # originale, che per un link diretto a un .mp4 funziona.
    if scraped is None:
        scraped = await apify.resolve_video(video_url)

    media_url = scraped.media_url if scraped is not None else video_url
    thumbnail_url = scraped.thumbnail_url if scraped is not None else None
    known_duration = scraped.duration_seconds if scraped is not None else None

    async with download_to_temp(
        media_url, settings, known_duration_seconds=known_duration
    ) as video:
        logger.info(
            "Analisi avviata: %.1f MB, durata %s, modalità %s",
            video.size_bytes / 1024 / 1024,
            f"{video.duration_seconds:.0f}s" if video.duration_seconds else "n/d",
            mode,
        )
        analysis = await gemini.analyze_video(video.path, video.mime_type, mode)

    payload = _build_insight_payload(
        analysis,
        video_url=video_url,
        mode=mode,
        creator_id=creator_id,
        thumbnail_url=thumbnail_url,
    )

    # Scrittura col client service-role: l'inferenza può concludersi dopo la
    # scadenza del JWT dell'utente, e nel percorso cron un JWT non esiste
    # affatto. `service_table` impone il filtro su user_id.
    insights = await service_table("insights", user_id)
    async with db_errors("upsert insight"):
        result = await (
            insights.upsert(payload, on_conflict="user_id,video_url").execute()
        )

    if not result.data:
        # L'upsert non ha restituito la rappresentazione: rileggiamo la riga
        # invece di restituire un record parziale.
        stored = await find_cached_insight(
            user_id, video_url, access_token=access_token, settings=settings
        )
        if stored is None:
            raise RuntimeError("L'insight non risulta salvato dopo l'upsert.")
        return stored, False

    return InsightResponse.model_validate(result.data[0]), False


# =============================================================================
# Endpoint
# =============================================================================


@router.post(
    "/analyze-video",
    response_model=InsightResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Analizza un video e restituisce l'insight",
    responses={
        200: {"description": "Video già analizzato: restituito il record in cache."},
        401: {"description": "JWT assente o non valido."},
        422: {"description": "URL non valido, oppure video oltre i limiti di dimensione o durata."},
        503: {"description": "Gemini o Apify non disponibili."},
    },
)
async def analyze_video(
    payload: AnalyzeVideoRequest,
    user: CurrentUser,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    gemini: Annotated[GeminiService, Depends(get_gemini_service)],
    apify: Annotated[ApifyService, Depends(get_apify_service)],
) -> InsightResponse:
    """Analizza il video indicato per conto dell'utente autenticato.

    `user_id` viene dal JWT verificato: il body non lo contiene e non potrebbe
    comunque influenzarlo.
    """
    video_url = normalize_video_url(payload.video_url)

    record, from_cache = await perform_analysis(
        user_id=user.id,
        video_url=video_url,
        mode=payload.analysis_mode,
        settings=settings,
        gemini=gemini,
        apify=apify,
        access_token=user.access_token,
    )

    # 201 solo quando l'analisi è stata effettivamente eseguita.
    response.status_code = status.HTTP_200_OK if from_cache else status.HTTP_201_CREATED
    return record
