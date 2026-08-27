"""Uno scraper per piattaforma, un solo tipo di ritorno.

Il resto dell'applicazione non sa da dove arrivi un video: chiede
`scrape_content(url)` e riceve uno `ScraperResult`, oppure `None` quando l'URL
non appartiene a una piattaforma nota — un link diretto a un `.mp4`, per
esempio, che si scarica e basta.

## Le tre piattaforme non si somigliano

* **YouTube** — Gemini accetta l'URL così com'è (`file_data`) e scarica lato
  Google. Niente byte da noi, niente Files API, latenza minima. I metadati
  arrivano dalla Data API v3, che è gratuita entro quota e non richiede uno
  scraper.
* **Instagram** — nessun passthrough nativo. Serve Apify per ottenere l'URL del
  file sulla CDN, e da lì il percorso classico: download, upload sulla Files
  API, riferimento nella `generateContent`.
* **TikTok** — come Instagram. L'actor era già nel progetto per il cron, quindi
  qui non si aggiunge alcuna dipendenza nuova.

## Il passthrough YouTube ha una condizione, e non è opzionale

Non scaricando il file, `download_to_temp` non viene eseguito — ed è **lui** ad
applicare `MAX_VIDEO_MB` e `MAX_VIDEO_DURATION_SECONDS`. Un passthrough
incondizionato toglierebbe quindi il tetto di durata proprio sulla piattaforma
dove è più facile incontrare un video di tre ore, e la spesa non sarebbe una
CDN ma l'inferenza Gemini.

Per questo il passthrough si usa **solo** quando `videos.list` ha confermato una
durata entro il limite. Senza chiave API, o con una durata non ricavabile, si
ricade sul percorso Apify + download, che i limiti li applica come sempre. Il
comportamento peggiore possibile è quindi quello che il progetto aveva già.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.core.config import Settings
from app.core.exceptions import VideoTooLongError
from app.schemas.creators import Platform
from app.schemas.scraping import ScraperResult
from app.services.apify_service import ApifyService, ScrapedVideo, detect_platform
from app.services.youtube_service import YouTubeService, video_id_from_url

logger = logging.getLogger(__name__)


class ContentScraper(Protocol):
    """Cosa deve saper fare uno scraper: un URL, un risultato normalizzato."""

    async def scrape(self, url: str) -> ScraperResult | None: ...


def da_video_scrapato(
    video: ScrapedVideo,
    platform: Platform,
    *,
    settings: Settings,
    passthrough: bool = True,
) -> ScraperResult:
    """Adatta la normalizzazione Apify all'interfaccia comune.

    Pubblica perché la usa anche il job cron: `fetch_latest_videos` gli
    restituisce già dei `ScrapedVideo`, e rinormalizzarli con un secondo
    convertitore parallelo significherebbe due posti in cui inseguire i cambi di
    nomenclatura degli actor.

    **Anche qui YouTube può passare in passthrough**, e alla stessa condizione:
    durata nota ed entro il limite. Senza questo ramo il cron continuerebbe a
    scaricare i video YouTube uno per uno — la spesa che il passthrough esiste
    per togliere, lasciata proprio sul percorso che ne analizza di più.

    `passthrough=False` lo esclude a priori, e serve a un caso solo: il ripiego
    di `YouTubeContentScraper`, dove si arriva **perché** la durata non era
    verificabile alla fonte. Riammettere il passthrough sulla base di un
    metadato di seconda mano annullerebbe la verifica appena fallita, ed è
    esattamente il buco che il ripiego esiste per chiudere.
    """
    durata = video.duration_seconds
    entro_il_limite = durata is not None and durata <= settings.max_video_duration_seconds

    comune: dict[str, Any] = {
        "platform": platform,
        "caption": video.caption or "",
        "like_count": video.like_count,
        "view_count": video.view_count,
        "comment_count": video.comment_count,
        "author_username": video.author_username or "",
        "posted_at": video.published_at,
        "thumbnail_url": video.thumbnail_url,
        "duration_seconds": durata,
    }

    if passthrough and platform == "youtube_shorts" and entro_il_limite:
        return ScraperResult(youtube_url=video.video_url, **comune)
    return ScraperResult(video_bytes_url=video.media_url, **comune)


# =============================================================================
# UN CONTROLLO MAI FATTO, SU DUE DEI TRE PERCORSI
# =============================================================================
# `ApifyService._single_video_input` passa `shouldDownloadVideos: true` **solo**
# per TikTok, dove serviva davvero: senza quel flag l'actor non restituiva alcun
# URL scaricabile, solo `webVideoUrl`, cioè la pagina HTML del post. Verificato
# chiamando l'actor direttamente — vedi il commento accanto al flag.
#
# Gli altri due input non lo passano, e **non sono mai stati controllati per lo
# stesso problema**. Non è una diagnosi: è l'assenza di una verifica, annotata
# qui perché chi tocca questi percorsi ci passa per forza.
#
# - **Instagram**: `ApifyContentScraper.scrape` → `resolve_video` →
#   `_single_video_input` è l'**unico** percorso che ha, non c'è passthrough.
#   Quindi il dubbio riguarda **tutto** il suo utilizzo, non un sottoinsieme.
# - **YouTube**: di norma va in passthrough (`YouTubeContentScraper.scrape`) e lì
#   non gli serve alcun URL scaricabile — non è a rischio. Ma ha un **ramo di
#   fallback ad Apify** quando la durata non è verificabile, ed è **quel ramo**,
#   non tutto YouTube, a non essere mai stato controllato.
#
# Questa nota è già stata corretta due volte sul codice reale: la prima versione
# dava YouTube per intero come a rischio, la seconda invertiva la struttura e
# attribuiva a Instagram la distinzione passthrough/fallback, che è solo di
# YouTube. Non riattribuirgliela una terza volta.
#
# (Veniva da `context.md`, dove era stato uno stato attivo per settimane senza
# essere né verificato né chiuso. Sta meglio qui: è una cautela sul codice, non
# un task.)


class ApifyContentScraper:
    """Instagram e TikTok: entrambi passano da un actor e dai byte del file.

    Una sola classe per due piattaforme e non due sottoclassi identiche: ciò che
    cambia — l'actor, la forma dell'input — è già dentro `ApifyService`, e
    duplicare qui la gerarchia significherebbe due file che non dicono nulla di
    diverso.
    """

    def __init__(self, apify: ApifyService, platform: Platform, settings: Settings) -> None:
        self._apify = apify
        self._platform = platform
        self._settings = settings

    async def scrape(self, url: str) -> ScraperResult | None:
        video = await self._apify.resolve_video(url, self._platform)
        if video is None:
            # Best effort, come prima: il chiamante tenta comunque il download
            # dell'URL originale, che per un link diretto a un file funziona.
            logger.info("Scraping non riuscito per %s: si procede senza metadati.", url)
            return None
        return da_video_scrapato(video, self._platform, settings=self._settings)


class YouTubeContentScraper:
    """YouTube: metadati dalla Data API, video passato per URL.

    Il ripiego su Apify non è un dettaglio implementativo ma la condizione che
    rende sicuro il passthrough — vedi la docstring del modulo.
    """

    def __init__(self, apify: ApifyService, youtube: YouTubeService, settings: Settings) -> None:
        self._apify = apify
        self._youtube = youtube
        self._settings = settings

    async def scrape(self, url: str) -> ScraperResult | None:
        video_id = video_id_from_url(url)
        metadati = await self._youtube.fetch_video(video_id) if video_id else None

        if metadati is not None and metadati.duration_seconds is not None:
            if metadati.duration_seconds > self._settings.max_video_duration_seconds:
                # Rifiuto **prima** di pagare: sul passthrough non c'è un
                # download da interrompere a metà, quindi questo è l'unico
                # momento in cui il limite può essere applicato.
                raise VideoTooLongError(
                    f"Il video dura {metadati.duration_seconds:.0f} secondi e supera il "
                    f"limite di {self._settings.max_video_duration_seconds} secondi."
                )

            logger.info(
                "YouTube: passthrough diretto a Gemini per %s (%.0fs).",
                metadati.video_id,
                metadati.duration_seconds,
            )
            return ScraperResult(
                platform="youtube_shorts",
                youtube_url=url,
                # YouTube non ha una didascalia separata dal titolo: il titolo
                # *è* il testo del post, ed è ciò che su Instagram sta in
                # `caption`.
                caption=metadati.title,
                like_count=metadati.like_count,
                view_count=metadati.view_count,
                comment_count=metadati.comment_count,
                # `videos.list` espone il **nome** del canale, non l'handle:
                # per quello servirebbe una `channels.list` sul `channelId`,
                # cioè una seconda unità di quota per ogni analisi. Qui vale
                # come identificazione leggibile dell'autore, non come handle
                # da passare a `POST /creators`.
                author_username=metadati.channel_title,
                posted_at=metadati.published_at,
                thumbnail_url=metadati.thumbnail_url,
                duration_seconds=metadati.duration_seconds,
            )

        # Durata sconosciuta: il passthrough salterebbe il limite, quindi si
        # torna al percorso che lo applica davvero.
        #
        # È **questo** ramo — non tutto YouTube — quello che non è mai stato
        # controllato per il parametro Apify mancante: vedi la nota in testa a
        # `ApifyContentScraper`.
        logger.info(
            "YouTube: durata non verificabile per %s, si passa da Apify e download.", url
        )
        video = await self._apify.resolve_video(url, "youtube_shorts")
        if video is None:
            return None
        return da_video_scrapato(
            video, "youtube_shorts", settings=self._settings, passthrough=False
        )


def get_scraper(
    platform: Platform,
    *,
    apify: ApifyService,
    youtube: YouTubeService,
    settings: Settings,
) -> ContentScraper:
    """Lo scraper della piattaforma indicata."""
    if platform == "youtube_shorts":
        return YouTubeContentScraper(apify, youtube, settings)
    return ApifyContentScraper(apify, platform, settings)


async def scrape_content(
    url: str,
    *,
    apify: ApifyService,
    youtube: YouTubeService,
    settings: Settings,
) -> ScraperResult | None:
    """Scraping del video all'URL indicato, `None` se non c'è nulla da dire.

    `None` non è un errore ed è un caso previsto: un link diretto a un `.mp4`
    non appartiene ad alcuna piattaforma, non ha metadati da recuperare, e il
    chiamante lo scarica com'è. È il comportamento che il progetto aveva già
    prima che questo modulo esistesse.
    """
    platform = detect_platform(url)
    if platform is None:
        return None

    scraper = get_scraper(
        platform,  # type: ignore[arg-type]  # detect_platform torna un valore di Platform
        apify=apify,
        youtube=youtube,
        settings=settings,
    )
    return await scraper.scrape(url)
