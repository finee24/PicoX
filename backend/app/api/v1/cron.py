"""`POST /api/v1/cron/check-updates` — controllo periodico dei creator attivi.

Autenticato con il segreto condiviso `X-CRON-SECRET`: non c'è un utente, quindi
non c'è un JWT. Gli `user_id` si ricavano dalle righe di `creators`, mai da un
parametro della richiesta.

Il giro è **resiliente per singolo creator**: se Apify fallisce o va in rate
limit su un creator, l'errore viene registrato e si passa al successivo. Un
fallimento parziale non deve azzerare il lavoro degli altri, e per l'utente
resta comunque disponibile il submit manuale via `POST /analyze-video`.

Le analisi vere e proprie non avvengono qui: la risposta torna subito con il
riepilogo, e i video nuovi vengono analizzati in background.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends

from app.api.v1.analyze import perform_analysis
from app.core.config import Settings, get_settings
from app.core.exceptions import ApifyError, PicoxError
from app.core.security import verify_cron_secret
from app.middleware.error_handler import SafeRoute
from app.schemas.analysis import AnalysisMode
from app.schemas.insights import CronCreatorResult, CronRunResponse
from app.services.apify_service import ApifyService, ScrapedVideo, get_apify_service
from app.services.gemini_service import GeminiService, get_gemini_service
from app.services.supabase_service import (
    db_errors,
    service_table,
    unscoped_service_table,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/cron",
    tags=["cron"],
    route_class=SafeRoute,
    dependencies=[Depends(verify_cron_secret)],
)


@dataclass(slots=True)
class _AnalysisJob:
    """Un'analisi da eseguire dopo l'invio della risposta."""

    user_id: str
    creator_id: str
    video_url: str
    mode: AnalysisMode
    scraped: ScrapedVideo


async def _load_active_creators() -> list[dict[str, Any]]:
    """Tutti i creator attivi, di tutti gli utenti.

    È l'**unica** query service-role senza filtro su `user_id` dell'applicazione:
    il cron non ha un utente corrente da cui derivarlo. Da qui in poi ogni
    operazione riparte dallo `user_id` letto sulla riga del creator e passa da
    `service_table`, che lo impone.
    """
    creators = await unscoped_service_table(
        "creators",
        reason="job cron: itera sui creator attivi di tutti gli utenti",
    )
    async with db_errors("select active creators"):
        result = await (
            creators.select("id, user_id, username, platform, analysis_mode")
            .eq("is_active", True)
            .order("created_at", desc=False)
            .execute()
        )
    # PostgREST tipizza `data` come JSON generico; qui le righe sono oggetti.
    return [row for row in (result.data or []) if isinstance(row, dict)]


async def _filter_already_analyzed(user_id: str, video_urls: list[str]) -> list[str]:
    """Rimuove gli URL già presenti in `insights` per quell'utente.

    Il dedup è per `(user_id, video_url)`, come il vincolo di unicità: lo stesso
    video analizzato da un altro utente non conta come già analizzato per questo.
    """
    if not video_urls:
        return []

    insights = await service_table("insights", user_id)
    async with db_errors("dedup insights"):
        result = await (
            insights.select("video_url").in_("video_url", video_urls).execute()
        )

    existing = {
        str(row["video_url"])
        for row in (result.data or [])
        if isinstance(row, dict) and row.get("video_url")
    }
    return [url for url in video_urls if url not in existing]


async def _run_job(
    job: _AnalysisJob,
    settings: Settings,
    gemini: GeminiService,
    apify: ApifyService,
) -> None:
    """Esegue una singola analisi in background.

    Non solleva mai: siamo dopo l'invio della risposta, quindi non c'è nessuno a
    cui riportare l'errore se non i log.
    """
    try:
        _, from_cache = await perform_analysis(
            user_id=job.user_id,
            video_url=job.video_url,
            mode=job.mode,
            settings=settings,
            gemini=gemini,
            apify=apify,
            creator_id=job.creator_id,
            scraped=job.scraped,
        )
        logger.info(
            "Cron: analisi %s per il creator %s (%s)",
            "già presente" if from_cache else "completata",
            job.creator_id,
            job.video_url,
        )
    except PicoxError as exc:
        logger.warning(
            "Cron: analisi fallita per %s (creator %s): [%s] %s",
            job.video_url,
            job.creator_id,
            exc.code,
            exc.message,
        )
    except Exception:  # noqa: BLE001 — nessun errore deve uccidere il worker
        logger.exception("Cron: analisi fallita in modo imprevisto per %s", job.video_url)


async def _run_queued_analyses(
    jobs: list[_AnalysisJob],
    settings: Settings,
    gemini: GeminiService,
    apify: ApifyService,
) -> None:
    """Esegue le analisi accodate con parallelismo limitato.

    Un solo `BackgroundTask` che ventaglia internamente, invece di N task: le
    analisi sono costose (download + inferenza) e lanciarle tutte insieme
    saturerebbe banda e quota Gemini.
    """
    semaphore = asyncio.Semaphore(settings.cron_max_concurrent_analyses)

    async def _guarded(job: _AnalysisJob) -> None:
        async with semaphore:
            await _run_job(job, settings, gemini, apify)

    logger.info("Cron: avvio di %s analisi in background.", len(jobs))
    await asyncio.gather(*(_guarded(job) for job in jobs))
    logger.info("Cron: analisi in background completate.")


@router.post(
    "/check-updates",
    response_model=CronRunResponse,
    summary="Controlla i nuovi video dei creator attivi",
    responses={
        401: {"description": "Header X-CRON-SECRET assente o errato."},
        503: {"description": "Il database non è raggiungibile."},
    },
)
async def check_updates(
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    gemini: Annotated[GeminiService, Depends(get_gemini_service)],
    apify: Annotated[ApifyService, Depends(get_apify_service)],
) -> CronRunResponse:
    """Cerca video nuovi per ogni creator attivo e ne accoda l'analisi.

    Risponde appena il censimento è concluso: le analisi proseguono in
    background. Il riepilogo dice, per ogni creator, quanti video sono stati
    trovati, quanti erano nuovi e quanti sono stati accodati.
    """
    creators = await _load_active_creators()
    logger.info("Cron: %s creator attivi da controllare.", len(creators))

    results: list[CronCreatorResult] = []
    jobs: list[_AnalysisJob] = []

    for creator in creators:
        creator_id = str(creator["id"])
        user_id = str(creator["user_id"])
        username = creator["username"]
        platform = creator["platform"]
        mode: AnalysisMode = creator.get("analysis_mode") or "BOTH"

        try:
            videos = await apify.fetch_latest_videos(platform, username)
        except ApifyError as exc:
            # Fallimento isolato: si registra e si prosegue con gli altri creator.
            logger.warning(
                "Cron: scraping fallito per %s/%s — %s", platform, username, exc.code
            )
            results.append(
                CronCreatorResult(
                    creator_id=creator["id"],
                    username=username,
                    platform=platform,
                    status="failed",
                    error=exc.message,
                )
            )
            continue
        except Exception:  # noqa: BLE001 — un creator non deve abortire il giro
            logger.exception("Cron: errore imprevisto su %s/%s", platform, username)
            results.append(
                CronCreatorResult(
                    creator_id=creator["id"],
                    username=username,
                    platform=platform,
                    status="failed",
                    error="Errore imprevisto durante lo scraping.",
                )
            )
            continue

        by_url = {video.video_url: video for video in videos}
        try:
            new_urls = await _filter_already_analyzed(user_id, list(by_url))
        except PicoxError as exc:
            logger.warning("Cron: dedup fallito per il creator %s — %s", creator_id, exc.code)
            results.append(
                CronCreatorResult(
                    creator_id=creator["id"],
                    username=username,
                    platform=platform,
                    status="failed",
                    videos_found=len(videos),
                    error="Verifica dei duplicati non riuscita.",
                )
            )
            continue

        for url in new_urls:
            jobs.append(
                _AnalysisJob(
                    user_id=user_id,
                    creator_id=creator_id,
                    video_url=url,
                    mode=mode,
                    scraped=by_url[url],
                )
            )

        results.append(
            CronCreatorResult(
                creator_id=creator["id"],
                username=username,
                platform=platform,
                status="ok",
                videos_found=len(videos),
                new_videos=len(new_urls),
                queued=len(new_urls),
            )
        )

    if jobs:
        background_tasks.add_task(_run_queued_analyses, jobs, settings, gemini, apify)

    return CronRunResponse(
        checked_creators=len(creators),
        failed_creators=sum(1 for r in results if r.status == "failed"),
        queued_analyses=len(jobs),
        results=results,
    )
