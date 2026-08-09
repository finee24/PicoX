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

import asyncio
import logging
import time
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.core.exceptions import AnalysisInProgressError
from app.core.security import CurrentUser
from app.middleware.error_handler import SafeRoute
from app.schemas.analysis import AnalysisMode, AnalyzeVideoRequest, VideoAnalysisResponse
from app.schemas.insights import InsightResponse
from app.services.analysis_lock import analysis_lock
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

    **`creator_id` fa eccezione e viene omesso quando è `None`**, e la ragione è
    l'opposto della stabilità: l'upsert genera `ON CONFLICT DO UPDATE SET` sulle
    sole colonne presenti nel payload, quindi una chiave assente lascia intatto
    il valore già in archivio. Includerla a `None` significherebbe cancellare
    l'attribuzione al creator.

    Non è un caso di scuola né richiede concorrenza. Il job cron scrive
    l'insight *con* il creator (`cron.py`); il path manuale non ha un creator da
    passare (`analyze.py`, endpoint `analyze-video`). Basta quindi che l'utente
    richieda a mano un video già analizzato dal cron, in una modalità che la
    riga esistente non copre, perché la rianalisi ne cancelli il creator —
    senza errore, senza log, e con l'attribuzione storica persa per sempre,
    dato che `insights` non denormalizza l'handle.
    """
    payload: dict[str, Any] = {
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

    if creator_id:
        payload["creator_id"] = str(creator_id)
    return payload


async def _assicura_attribuzione(
    record: InsightResponse,
    user_id: str,
    creator_id: UUID | str | None,
) -> InsightResponse:
    """Aggancia il creator a un insight che ne è privo, su un cache hit.

    Omettere `creator_id` dall'upsert impedisce di **cancellare** l'attribuzione,
    ma non basta: se due richieste corrono sullo stesso video e vince quella
    manuale — che un creator non ce l'ha — l'analisi viene scritta senza, e chi
    aveva il creator riceve quel risultato come cache hit senza mai scriverlo.
    Nessuno ha sovrascritto nulla, eppure l'attribuzione non c'è.

    E non si recupererebbe da sé: il cron salta i video che hanno già un
    insight (`_filter_already_analyzed`), quindi non tornerebbe mai su quella
    riga. L'attribuzione andrebbe persa per sempre in silenzio, che è
    esattamente il difetto da cui questa modifica nasce.

    L'aggiornamento è mirato e non tocca altro: solo la colonna, solo se è
    ancora vuota, filtrando su PK **e** proprietario.
    """
    if not creator_id or record.creator_id is not None:
        return record

    insights = await service_table("insights", user_id)
    async with db_errors("attribuzione del creator su insight esistente"):
        result = await (
            insights.update({"creator_id": str(creator_id)})
            .eq("id", str(record.id))
            .is_("creator_id", "null")
            .execute()
        )

    if not result.data:
        # Un'altra richiesta l'ha attribuito nel frattempo: va bene così.
        return record

    logger.info(
        "Attribuzione del creator %s aggiunta all'insight %s.", creator_id, record.id
    )
    return InsightResponse.model_validate(result.data[0])


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

    Il lavoro vero è protetto da un lock su `(user_id, video_url, mode)`: senza,
    N richieste concorrenti sullo stesso video producono N inferenze Gemini e N
    run Apify per finire in un'unica riga, cioè si paga N volte un risultato
    solo. Chi non ottiene il lock non duplica la spesa: attende che il
    detentore finisca e ne restituisce il risultato.
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
        return await _assicura_attribuzione(cached, user_id, creator_id), True

    async with analysis_lock(user_id, video_url, mode, settings) as ottenuto:
        if not ottenuto:
            atteso, _ = await _attendi_analisi_altrui(
                user_id,
                video_url,
                mode,
                access_token=access_token,
                settings=settings,
            )
            return await _assicura_attribuzione(atteso, user_id, creator_id), True

        # Ricontrollo dopo l'acquisizione. Fra il lookup qui sopra e il lock può
        # essere passata un'altra richiesta che ha completato l'analisi: senza
        # questa seconda lettura la pagheremmo di nuovo, che è esattamente ciò
        # che il lock esiste per evitare.
        cached = await find_cached_insight(
            user_id,
            video_url,
            access_token=access_token,
            settings=settings,
            required_mode=mode,
        )
        if cached is not None:
            logger.info(
                "Analisi già completata da una richiesta concorrente per l'utente %s",
                user_id,
            )
            return await _assicura_attribuzione(cached, user_id, creator_id), True

        return await _esegui_analisi(
            user_id=user_id,
            video_url=video_url,
            mode=mode,
            settings=settings,
            gemini=gemini,
            apify=apify,
            access_token=access_token,
            creator_id=creator_id,
            scraped=scraped,
        )


async def _attendi_analisi_altrui(
    user_id: str,
    video_url: str,
    mode: AnalysisMode,
    *,
    access_token: str | None,
    settings: Settings,
) -> tuple[InsightResponse, bool]:
    """Attende che chi detiene il lock finisca, e ne restituisce il risultato.

    L'attesa è un `await` su un poll, non un thread occupato: il costo di
    aspettare è molto più basso di quanto suggerirebbe il fallimento immediato,
    e in cambio un doppio click torna a essere invisibile all'utente invece di
    diventare un errore.

    Il timeout è deliberatamente scorrelato dal TTL del lock: quello è un
    vincolo di correttezza — deve superare la durata massima di un'analisi,
    altrimenti il mutex si rompe — mentre questo è una scelta di esperienza
    utente. Scaduto, si risponde 409: l'analisi altrui prosegue comunque, e una
    richiesta successiva la troverà in cache.
    """
    scadenza = time.monotonic() + settings.analysis_lock_wait_seconds
    logger.info(
        "Analisi già in corso per %s (modalità %s): attesa del risultato.",
        video_url,
        mode,
    )

    while True:
        await asyncio.sleep(settings.analysis_lock_poll_seconds)

        pronto = await find_cached_insight(
            user_id,
            video_url,
            access_token=access_token,
            settings=settings,
            required_mode=mode,
        )
        if pronto is not None:
            return pronto, True

        if time.monotonic() >= scadenza:
            raise AnalysisInProgressError()


async def _esegui_analisi(
    *,
    user_id: str,
    video_url: str,
    mode: AnalysisMode,
    settings: Settings,
    gemini: GeminiService,
    apify: ApifyService,
    access_token: str | None,
    creator_id: UUID | str | None,
    scraped: ScrapedVideo | None,
) -> tuple[InsightResponse, bool]:
    """La pipeline vera, eseguita col lock in mano."""
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
        409: {
            "description": (
                "Un'altra richiesta sta già analizzando questo video per lo stesso "
                "utente e non ha finito entro l'attesa massima."
            )
        },
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
