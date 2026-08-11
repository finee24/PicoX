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
from weakref import WeakKeyDictionary

from fastapi import APIRouter, BackgroundTasks, Depends

from app.api.v1.analyze import perform_analysis
from app.core.config import Settings, get_settings
from app.core.exceptions import ApifyError, PicoxError
from app.core.security import verify_cron_enabled, verify_cron_secret
from app.middleware.error_handler import SafeRoute
from app.schemas.analysis import AnalysisMode
from app.schemas.insights import CronCreatorResult, CronRunResponse
from app.services.apify_service import ApifyService, ScrapedVideo, get_apify_service
from app.services.gemini_service import GeminiService, get_gemini_service
from app.services.job_lock import CRON_CHECK_UPDATES, acquire, release
from app.services.media_service import canonical_cache_key
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
    # L'ORDINE CONTA. `verify_cron_enabled` sta **dopo** l'autenticazione perché
    # le dipendenze di router sono valutate nell'ordine in cui compaiono:
    # invertendole, chiunque senza segreto potrebbe distinguere `503
    # cron_disabled` da `401` e dedurre lo stato di configurazione dell'istanza.
    dependencies=[Depends(verify_cron_secret), Depends(verify_cron_enabled)],
)

# Semaforo **di processo**, non per esecuzione.
#
# Prima stava dentro `_run_queued_analyses`, quindi ogni esecuzione ne
# costruiva uno proprio e `CRON_MAX_CONCURRENT_ANALYSES` limitava la singola
# richiesta invece del processo: due giri sovrapposti raddoppiavano il
# parallelismo (misurato: picco 4 con limite configurato 2). Qui il limite vale
# per tutto il processo, anche se un giro precedente sta ancora finendo le
# proprie analisi.
#
# Resta per-processo e non per-cluster: coordinare il parallelismo fra istanze
# diverse è un requisito differente, che oggi non esiste.
#
# È indicizzato sull'**event loop**, non una variabile singola. Un
# `asyncio.Semaphore` si lega al loop al primo `await`, e riusarlo da un loop
# diverso solleva `RuntimeError: bound to a different event loop`. In produzione
# il loop è uno solo per tutta la vita del processo, quindi la distinzione non
# si vedrebbe mai — ma un singolo oggetto globale renderebbe il modulo
# inutilizzabile da qualunque contesto che crei un loop proprio, e la
# `WeakKeyDictionary` costa cinque righe e nessuna fixture di test che nasconda
# il problema invece di risolverlo. Le voci spariscono con il loop che le ha
# create: nessuna pulizia da ricordare.
_semafori: WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]
] = WeakKeyDictionary()


def _semaforo_analisi(limite: int) -> asyncio.Semaphore:
    """Il semaforo condiviso dal processo, uno per event loop.

    Ricostruito se il limite configurato cambia — cosa che in esercizio non
    accade, ma nei test sì, dove ogni caso può impostare il proprio valore.
    """
    loop = asyncio.get_running_loop()
    voce = _semafori.get(loop)
    if voce is None or voce[0] != limite:
        voce = (limite, asyncio.Semaphore(limite))
        _semafori[loop] = voce
    return voce[1]


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

    Il dedup è per `(user_id, cache_key)`, **la stessa chiave** del vincolo di
    unicità, del lock e della cache di lettura. Confrontare gli URL grezzi
    riaccoderebbe un video già analizzato quando lo scraper lo restituisce sotto
    una forma diversa da quella archiviata — e riaccodarlo significa ripagarlo,
    sul percorso automatico invece che manuale.

    Lo scoping resta per utente: lo stesso video analizzato da un altro account
    non conta come già analizzato per questo.
    """
    if not video_urls:
        return []

    per_chiave = {canonical_cache_key(url): url for url in video_urls}

    insights = await service_table("insights", user_id)
    async with db_errors("dedup insights"):
        result = await (
            insights.select("cache_key").in_("cache_key", list(per_chiave)).execute()
        )

    presenti = {
        str(row["cache_key"])
        for row in (result.data or [])
        if isinstance(row, dict) and row.get("cache_key")
    }
    # Si riparte dagli URL originali, non dalle chiavi: il chiamante deve poter
    # risalire ai metadati già scaricati con `by_url`.
    return [url for chiave, url in per_chiave.items() if chiave not in presenti]


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
            # Il cron non consuma la quota manuale dell'utente: ha gia' il
            # proprio budget, imposto dal tetto ai creator attivi. Due budget
            # indipendenti, ognuno con un proprietario chiaro — altrimenti un
            # giro notturno azzererebbe le analisi che l'utente puo' fare di
            # giorno, senza che nulla glielo spieghi.
            conta_quota=False,
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
    except Exception:
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
    async def _guarded(job: _AnalysisJob) -> None:
        async with _semaforo_analisi(settings.cron_max_concurrent_analyses):
            await _run_job(job, settings, gemini, apify)

    logger.info("Cron: avvio di %s analisi in background.", len(jobs))
    await asyncio.gather(*(_guarded(job) for job in jobs))
    logger.info("Cron: analisi in background completate.")


async def _esegui_e_rilascia(
    jobs: list[_AnalysisJob],
    settings: Settings,
    gemini: GeminiService,
    apify: ApifyService,
) -> None:
    """Le analisi in background, e **poi** il rilascio del lock.

    Il rilascio vive qui e non nella route perche' il lock copre l'intero giro,
    non la sola richiesta HTTP. `BackgroundTasks` disaccoppia le due cose: la
    risposta 200 parte a fine censimento, mentre le analisi proseguono per
    minuti. Rilasciando a fine richiesta, una seconda esecuzione troverebbe il
    lock libero e ripartirebbe mentre la prima sta ancora analizzando.

    `finally` e non una riga in fondo: se qualcosa esplodesse in modo imprevisto
    il lock deve tornare libero comunque, invece di restare occupato fino alla
    scadenza del TTL per un errore gia' gestito altrove.
    """
    try:
        await _run_queued_analyses(jobs, settings, gemini, apify)
    finally:
        await release(CRON_CHECK_UPDATES)


async def _censisci_creator(
    creator: dict[str, Any],
    apify: ApifyService,
    semaforo: asyncio.Semaphore,
) -> tuple[CronCreatorResult, list[_AnalysisJob]]:
    """Scraping e dedup di un singolo creator. Non solleva mai.

    Estratta dal ciclo per poter girare in parallelo: il censimento era
    sequenziale — un `await` per creator, con `APIFY_TIMEOUT_SECONDS = 180`
    ciascuno — e al tetto di 30 creator attivi sfiorava i 120s nel caso tipico,
    cioe' la soglia oltre la quale il client abortiva e ritentava. Era il retry,
    non lo schedule, la sorgente vera delle esecuzioni sovrapposte.

    Il semaforo tiene il parallelismo entro `CRON_CENSUS_CONCURRENCY`: senza,
    trenta run Apify simultanee sarebbero solo un modo diverso di farsi limitare.
    """
    creator_id = str(creator["id"])
    user_id = str(creator["user_id"])
    username = creator["username"]
    platform = creator["platform"]
    mode: AnalysisMode = creator.get("analysis_mode") or "BOTH"

    def fallito(errore: str, videos_found: int = 0) -> tuple[CronCreatorResult, list[_AnalysisJob]]:
        return (
            CronCreatorResult(
                creator_id=creator["id"],
                username=username,
                platform=platform,
                status="failed",
                videos_found=videos_found,
                error=errore,
            ),
            [],
        )

    async with semaforo:
        try:
            videos = await apify.fetch_latest_videos(platform, username)
        except ApifyError as exc:
            # Fallimento isolato: si registra e gli altri creator proseguono.
            logger.warning(
                "Cron: scraping fallito per %s/%s — %s", platform, username, exc.code
            )
            return fallito(exc.message)
        except Exception:
            logger.exception("Cron: errore imprevisto su %s/%s", platform, username)
            return fallito("Errore imprevisto durante lo scraping.")

        by_url = {video.video_url: video for video in videos}
        try:
            new_urls = await _filter_already_analyzed(user_id, list(by_url))
        except PicoxError as exc:
            logger.warning(
                "Cron: dedup fallito per il creator %s — %s", creator_id, exc.code
            )
            return fallito("Verifica dei duplicati non riuscita.", len(videos))

    jobs = [
        _AnalysisJob(
            user_id=user_id,
            creator_id=creator_id,
            video_url=url,
            mode=mode,
            scraped=by_url[url],
        )
        for url in new_urls
    ]
    return (
        CronCreatorResult(
            creator_id=creator["id"],
            username=username,
            platform=platform,
            status="ok",
            videos_found=len(videos),
            new_videos=len(new_urls),
            queued=len(new_urls),
        ),
        jobs,
    )


@router.post(
    "/check-updates",
    response_model=CronRunResponse,
    summary="Controlla i nuovi video dei creator attivi",
    responses={
        401: {"description": "Header X-CRON-SECRET assente o errato."},
        503: {
            "description": (
                "Il database non e' raggiungibile, oppure il cron non e' "
                "abilitato su questa istanza (`cron_disabled`)."
            )
        },
    },
)
async def check_updates(
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    gemini: Annotated[GeminiService, Depends(get_gemini_service)],
    apify: Annotated[ApifyService, Depends(get_apify_service)],
) -> CronRunResponse:
    """Cerca video nuovi per ogni creator attivo e ne accoda l'analisi.

    Un giro per volta, arbitrato da `job_locks`: chi arriva mentre un altro giro
    e' in corso **salta**, non attende. Un cron che si accoda e' peggio di uno
    che salta un giro, perche' la coda cresce mentre la finestra successiva
    arriva comunque.

    Risponde appena il censimento e' concluso: le analisi proseguono in
    background, e il lock resta preso insieme a loro.
    """
    if not await acquire(CRON_CHECK_UPDATES, settings.cron_lock_ttl_seconds):
        # `warning` e non `info`: saltare e' legittimo, ma se accade a ogni giro
        # significa che i giri durano piu' dello schedule, ed e' una cosa che
        # deve essere visibile senza andarla a cercare.
        logger.warning(
            "Cron: giro saltato, un'altra esecuzione e' gia' in corso "
            "(lock '%s' ancora valido).",
            CRON_CHECK_UPDATES,
        )
        return CronRunResponse(
            checked_creators=0,
            failed_creators=0,
            queued_analyses=0,
            results=[],
            skipped=True,
        )

    # Il lock passa di mano al background task solo se ce n'e' uno da avviare:
    # in ogni altro caso — nessuna analisi da fare, oppure un'eccezione durante
    # il censimento — va rilasciato qui.
    rilascia_qui = True
    try:
        creators = await _load_active_creators()
        logger.info("Cron: %s creator attivi da controllare.", len(creators))

        semaforo = asyncio.Semaphore(settings.cron_census_concurrency)
        censimento = await asyncio.gather(
            *(_censisci_creator(creator, apify, semaforo) for creator in creators)
        )

        results = [esito for esito, _ in censimento]
        jobs = [job for _, lavori in censimento for job in lavori]

        # La risposta si costruisce **prima** di cedere il lock: se fallisse
        # dopo `add_task`, il background non partirebbe — Starlette esegue i
        # task solo a risposta prodotta — e il lock resterebbe appeso fino al
        # TTL per un errore che non c'entra nulla con le analisi.
        risposta = CronRunResponse(
            checked_creators=len(creators),
            failed_creators=sum(1 for r in results if r.status == "failed"),
            queued_analyses=len(jobs),
            results=results,
        )

        if jobs:
            background_tasks.add_task(_esegui_e_rilascia, jobs, settings, gemini, apify)
            rilascia_qui = False

        return risposta
    finally:
        if rilascia_qui:
            await release(CRON_CHECK_UPDATES)
