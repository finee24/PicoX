"""La pipeline di analisi di un video, dall'URL all'insight scritto.

Vive qui e non nella route perche' non e' logica di presentazione: e' cio' che
decide se si spende, quanto, e cosa finisce sul database. Il segnale che era nel
posto sbagliato lo dava `cron.py`, che importava `perform_analysis` da un modulo
di route — un job pianificato che dipende da un endpoint HTTP per fare il
proprio lavoro.

I passi, in ordine:

1. normalizzazione dell'URL (chiave della cache e del vincolo di unicita');
2. **cache check** su `(user_id, cache_key)` col client scoped al JWT: se il
   video e' gia' stato analizzato si restituisce il record esistente senza
   toccare ne' Apify ne' Gemini;
3. risoluzione best effort dei metadati via Apify (URL diretto del media,
   thumbnail, durata);
4. download in un file temporaneo, con limiti di dimensione e durata e cleanup
   garantito — **salvo il passthrough YouTube**, dove a scaricare e' Google;
5. inferenza Gemini in structured output;
6. scrittura su `insights` col client service-role.

`perform_analysis` e' il punto d'ingresso, usato sia dalla route sia dal job
cron, che esegue gli stessi passi in background senza un JWT di sessione.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final
from uuid import UUID

from app.core.config import Settings
from app.core.exceptions import AnalysisInProgressError
from app.schemas.analysis import AnalysisMode, VideoAnalysisResponse
from app.schemas.insights import InsightResponse
from app.schemas.scraping import ScraperResult
from app.services.analysis_lock import analysis_lock
from app.services.apify_service import ApifyService
from app.services.content_scraper import scrape_content
from app.services.gemini_service import GeminiService
from app.services.media_service import (
    assert_public_target,
    canonical_cache_key,
    download_to_temp,
)
from app.services.prompt_loader import AnalysisContext
from app.services.supabase_service import db_errors, scoped_table, service_table
from app.services.youtube_service import YouTubeService

logger = logging.getLogger(__name__)


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
    cache_key: str,
    *,
    access_token: str | None,
    settings: Settings,
    required_mode: AnalysisMode | None = None,
) -> InsightResponse | None:
    """Cerca un insight già presente per `(user_id, cache_key)`.

    La chiave è `cache_key` e **non** `video_url` (migration `0009`): due forme
    dello stesso video hanno URL diversi ed è corretto che li abbiano, perché
    quello è il valore mostrato all'utente. Cercare per URL significherebbe non
    trovare in cache un video già analizzato sotto un'altra forma, e ripagarlo.

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
            async with scoped_table("insights", user_id, access_token, settings) as insights:
                result = await (
                    insights.select("*").eq("cache_key", cache_key).limit(1).execute()
                )
        else:
            servizio = await service_table("insights", user_id)
            result = await (
                servizio.select("*").eq("cache_key", cache_key).limit(1).execute()
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


def _componi_payload_insight(
    analysis: VideoAnalysisResponse,
    *,
    video_url: str,
    cache_key: str,
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
        # Due colonne, due mestieri: `video_url` è ciò che si mostra — finisce in
        # un `href` cliccabile — e `cache_key` è ciò che identifica. Vedi la
        # migration 0009.
        "video_url": video_url,
        "cache_key": cache_key,
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


async def _consuma_quota(user_id: str, video_url: str, mode: AnalysisMode) -> None:
    """Registra l'analisi che sta per partire, o la rifiuta se la quota e' finita.

    Va chiamata **dopo** i controlli di cache e **dentro** il lock, perche' e'
    li' che la spesa e' decisa. Le conseguenze di sbagliare il punto sono
    entrambe reali: piu' in alto si conterebbero i cache hit, che non costano
    nulla; piu' in basso non si conterebbero le analisi che pagano Apify e poi
    falliscono su Gemini — le stesse che non lasciano alcuna riga in `insights`,
    ed e' il motivo per cui contare gli insight avrebbe sottostimato l'abuso.

    Non decide nulla da se': l'arbitro e' il trigger `enforce_analysis_quota`
    (migration 0008), che solleva `PX002` tradotto qui in `AnalysisQuotaError`.
    Cosi' due richieste concorrenti al tetto non possono superarlo entrambe,
    cosa che un controllo applicativo leggi-poi-scrivi non garantirebbe.
    """
    eventi = await service_table("analysis_events", user_id)
    async with db_errors("quota giornaliera di analisi"):
        await eventi.insert({"video_url": video_url, "analysis_mode": mode}).execute()


async def perform_analysis(
    *,
    user_id: str,
    video_url: str,
    mode: AnalysisMode,
    settings: Settings,
    gemini: GeminiService,
    apify: ApifyService,
    youtube: YouTubeService,
    access_token: str | None = None,
    creator_id: UUID | str | None = None,
    scraped: ScraperResult | None = None,
    conta_quota: bool = True,
) -> tuple[InsightResponse, bool]:
    """Analizza un video e ne persiste il risultato.

    Restituisce `(record, from_cache)`. `scraped` permette al job cron di
    riutilizzare i metadati già ottenuti dallo scraping, evitando una seconda
    chiamata ad Apify per lo stesso video.

    `conta_quota` distingue i due chiamanti: il percorso manuale consuma la
    quota giornaliera dell'utente, il cron no. Il cron ha già il proprio budget
    — creator attivi per `apify_results_per_creator` — e farglielo consumare
    renderebbe il limite inspiegabile all'utente («perché non posso analizzare?
    perché stanotte è girato il cron»). È un parametro esplicito e non una
    deduzione da `creator_id` o `scraped`, che oggi separano i due chiamanti
    solo per convenzione.

    Il lavoro vero è protetto da un lock su `(user_id, cache_key, mode)`: senza,
    N richieste concorrenti sullo stesso video producono N inferenze Gemini e N
    run Apify per finire in un'unica riga, cioè si paga N volte un risultato
    solo. Chi non ottiene il lock non duplica la spesa: attende che il
    detentore finisca e ne restituisce il risultato.

    **La chiave si deriva qui, una volta sola.** `cache_key` non è un parametro:
    passarlo dall'esterno aprirebbe la possibilità che due chiamanti ne calcolino
    versioni diverse, ed è esattamente la divergenza che la migration `0009`
    esiste per chiudere — cache di lettura, upsert, lock e dedup del cron devono
    guardare la stessa stringa.
    """
    cache_key = canonical_cache_key(video_url)

    cached = await find_cached_insight(
        user_id,
        cache_key,
        access_token=access_token,
        settings=settings,
        required_mode=mode,
    )
    if cached is not None:
        logger.info("Cache hit su insights per l'utente %s", user_id)
        return await _assicura_attribuzione(cached, user_id, creator_id), True

    async with analysis_lock(user_id, cache_key, mode, settings) as ottenuto:
        if not ottenuto:
            atteso, _ = await _attendi_analisi_altrui(
                user_id,
                cache_key,
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
            cache_key,
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

        if conta_quota:
            await _consuma_quota(user_id, video_url, mode)

        return await _esegui_analisi(
            user_id=user_id,
            video_url=video_url,
            cache_key=cache_key,
            mode=mode,
            settings=settings,
            gemini=gemini,
            apify=apify,
            youtube=youtube,
            access_token=access_token,
            creator_id=creator_id,
            scraped=scraped,
        )


async def _attendi_analisi_altrui(
    user_id: str,
    cache_key: str,
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
        cache_key,
        mode,
    )

    while True:
        await asyncio.sleep(settings.analysis_lock_poll_seconds)

        pronto = await find_cached_insight(
            user_id,
            cache_key,
            access_token=access_token,
            settings=settings,
            required_mode=mode,
        )
        if pronto is not None:
            return pronto, True

        if time.monotonic() >= scadenza:
            raise AnalysisInProgressError()


async def run_analysis(
    scraped: ScraperResult | None,
    *,
    video_url: str,
    mode: AnalysisMode,
    settings: Settings,
    gemini: GeminiService,
) -> VideoAnalysisResponse:
    """Dal video all'analisi, **indipendentemente dalla piattaforma**.

    È l'unico punto in cui le piattaforme si distinguono, e la distinzione è una
    sola riga: c'è un `youtube_url` oppure no. Tutto ciò che le rende diverse —
    quale actor, quale API, quale forma di risposta — è già stato assorbito
    dallo scraper, e qui non se ne trova traccia.

    * **passthrough** — l'URL va a Gemini così com'è e a scaricarlo è Google.
      Non c'è alcun byte che passi da qui, quindi nemmeno un `download_to_temp`
      da cui far scattare i limiti: quelli li ha già applicati lo scraper, che
      per questo motivo non fa passthrough se non conosce la durata
      (`content_scraper`, docstring del modulo);
    * **byte** — download in un file temporaneo con i limiti di dimensione e
      durata, poi upload sulla Files API. È il percorso di Instagram e TikTok, e
      anche di un link diretto a un `.mp4`, per cui `scraped` è `None`.
    """
    # Caption, hashtag e autore entrano nel prompt come *contesto*, delimitato
    # come dato non fidato: li scrive il creator del video, non l'utente di
    # Picox (`prompt_loader`, docstring del modulo). Senza scraping riuscito non
    # c'è contesto da dare, e il prompt resta quello base.
    contesto = AnalysisContext.da_scraping(scraped) if scraped is not None else None

    if scraped is not None and scraped.youtube_url:
        # La stessa difesa SSRF del percorso di download, applicata anche qui.
        #
        # `assert_public_target` viveva solo dentro `download_to_temp`, cioè nel
        # ramo che i byte li scarica. Il passthrough quel ramo lo salta per
        # definizione — a scaricare è Google — e con esso saltava il controllo,
        # lasciando che un URL puntato alla rete interna arrivasse a Gemini senza
        # che nessuno lo guardasse. Che l'URL venga dallo scraper e non
        # dall'utente non basta a fidarsi: lo scraper restituisce ciò che il
        # provider gli dice, e un redirect è sufficiente a spostare la
        # destinazione.
        #
        # Solleva `PicoxValidationError` (422) o `ExternalServiceError` (503)
        # esattamente come sull'altro percorso, quindi il chiamante non ha un
        # caso nuovo da gestire.
        await assert_public_target(scraped.youtube_url)

        logger.info(
            "Analisi avviata in passthrough su %s, modalità %s",
            scraped.platform,
            mode,
        )
        return await gemini.analyze_video_url(
            scraped.youtube_url, mode, context=contesto
        )

    # Senza scraper riuscito si tenta comunque l'URL originale: per un link
    # diretto a un file funziona, ed è il comportamento che il progetto aveva
    # prima che gli scraper esistessero.
    media_url = (scraped.video_bytes_url if scraped is not None else None) or video_url
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
        return await gemini.analyze_video(
            video.path, video.mime_type, mode, context=contesto
        )


async def _esegui_analisi(
    *,
    user_id: str,
    video_url: str,
    cache_key: str,
    mode: AnalysisMode,
    settings: Settings,
    gemini: GeminiService,
    apify: ApifyService,
    youtube: YouTubeService,
    access_token: str | None,
    creator_id: UUID | str | None,
    scraped: ScraperResult | None,
) -> tuple[InsightResponse, bool]:
    """La pipeline vera, eseguita col lock in mano."""
    # Best effort: serve l'URL diretto del media, perché la pagina del post non
    # è un file video — tranne su YouTube, dove serve l'URL stesso e null'altro.
    # Un fallimento non è fatale: si tenta comunque l'URL originale.
    if scraped is None:
        scraped = await scrape_content(
            video_url, apify=apify, youtube=youtube, settings=settings
        )

    thumbnail_url = scraped.thumbnail_url if scraped is not None else None

    analysis = await run_analysis(
        scraped,
        video_url=video_url,
        mode=mode,
        settings=settings,
        gemini=gemini,
    )

    payload = _componi_payload_insight(
        analysis,
        video_url=video_url,
        cache_key=cache_key,
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
            # Il target segue il vincolo spostato dalla migration 0009. Il
            # fix dell'omissione di `creator_id` resta valido: dipende da quali
            # colonne stanno nel *payload*, non da quali stanno nel target, e
            # `ON CONFLICT DO UPDATE SET` continua a toccare solo le presenti.
            insights.upsert(payload, on_conflict="user_id,cache_key").execute()
        )

    if not result.data:
        # L'upsert non ha restituito la rappresentazione: rileggiamo la riga
        # invece di restituire un record parziale.
        stored = await find_cached_insight(
            user_id, cache_key, access_token=access_token, settings=settings
        )
        if stored is None:
            raise RuntimeError("L'insight non risulta salvato dopo l'upsert.")
        return stored, False

    return InsightResponse.model_validate(result.data[0]), False
