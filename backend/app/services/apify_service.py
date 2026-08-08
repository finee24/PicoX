"""Scraping dei video dei creator tramite Apify.

Un actor diverso per piattaforma, con schemi di input e di output tutti diversi
tra loro. Il modulo li riconduce a un unico tipo — `ScrapedVideo` — così che il
resto dell'applicazione non conosca né i nomi degli actor né la forma dei loro
dataset.

Il rate limit di Apify (429) è normale su un piano condiviso: viene tradotto in
`ApifyError` (503) e, nel job cron, gestito per singolo creator senza abortire
l'intero giro.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

from apify_client import ApifyClientAsync
from apify_client.errors import ApifyApiError, ApifyClientError

from app.core.config import Settings, get_settings
from app.core.exceptions import ApifyError
from app.services.media_service import normalize_video_url

logger = logging.getLogger(__name__)

_RATE_LIMIT_STATUS: Final = 429
_APIFY_STORAGE_HOST: Final = "api.apify.com"
_RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})

# Chiavi candidate per ciascun campo, in ordine di preferenza. Gli actor
# cambiano nomenclatura fra versioni: sondare più chiavi costa meno che
# inseguire ogni release con una migrazione.
_URL_KEYS: Final = ("url", "webVideoUrl", "postPage", "shortUrl", "link", "videoUrl")
_DOWNLOAD_KEYS: Final = (
    "videoUrlNoWaterMark",
    "downloadAddr",
    "videoUrl",
    "videoPlayUrl",
    "mediaUrl",
    "downloadUrl",
)
_THUMBNAIL_KEYS: Final = (
    "displayUrl",
    "thumbnailUrl",
    "thumbnail",
    "videoThumbnail",
    "coverUrl",
    "originalCoverUrl",
)
_DURATION_KEYS: Final = ("videoDuration", "duration", "durationSeconds", "lengthSeconds")
_CAPTION_KEYS: Final = ("caption", "text", "title", "description")
_PUBLISHED_KEYS: Final = ("timestamp", "createTimeISO", "uploadDate", "date", "publishedAt")


@dataclass(slots=True)
class ScrapedVideo:
    """Video restituito da uno scraper, normalizzato.

    `video_url` è l'URL canonico della pagina — la chiave usata per la cache e
    per il dedup. `download_url` è l'URL del file media, che scade e non va mai
    persistito come chiave.
    """

    video_url: str
    download_url: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: float | None = None
    caption: str | None = None
    published_at: datetime | None = None

    @property
    def media_url(self) -> str:
        """URL da cui scaricare effettivamente il file."""
        return self.download_url or self.video_url


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        # Alcuni actor restituiscono liste di URL (es. `mediaUrls`, `covers`).
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return None


def _first_float(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        if isinstance(value, str):
            try:
                parsed = float(value)
            except ValueError:
                continue
            if parsed > 0:
                return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _normalize_item(item: dict[str, Any]) -> ScrapedVideo | None:
    """Riduce un item di dataset a `ScrapedVideo`, o lo scarta se inutilizzabile."""
    # `videoMeta` (TikTok) annida durata e cover.
    raw_meta = item.get("videoMeta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}

    raw_url = _first_str(item, _URL_KEYS)
    if not raw_url:
        return None
    try:
        video_url = normalize_video_url(raw_url)
    except Exception:  # noqa: BLE001 — un item malformato non deve fermare il batch
        logger.debug("Item con URL non normalizzabile, scartato.")
        return None

    # L'URL resta pulito: se punta al key value store di Apify, la credenziale
    # la aggiunge il downloader in header, per hop — vedi
    # `media_service._per_hop_headers`. In query string finirebbe nel messaggio
    # di ogni eccezione httpx, e da lì nei log.
    download_url = _first_str(item, _DOWNLOAD_KEYS) or _first_str(meta, _DOWNLOAD_KEYS)

    return ScrapedVideo(
        video_url=video_url,
        download_url=download_url,
        thumbnail_url=_first_str(item, _THUMBNAIL_KEYS) or _first_str(meta, _THUMBNAIL_KEYS),
        duration_seconds=_first_float(item, _DURATION_KEYS) or _first_float(meta, _DURATION_KEYS),
        caption=_first_str(item, _CAPTION_KEYS),
        published_at=next(
            (dt for key in _PUBLISHED_KEYS if (dt := _parse_datetime(item.get(key)))), None
        ),
    )


class ApifyService:
    """Wrapper sugli actor di scraping."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = ApifyClientAsync(
            token=self._settings.apify_api_token.get_secret_value()
        )

    # --- Input specifici per actor ------------------------------------------

    def _actor_id(self, platform: str) -> str:
        match platform:
            case "instagram":
                return self._settings.apify_instagram_actor
            case "tiktok":
                return self._settings.apify_tiktok_actor
            case "youtube_shorts":
                return self._settings.apify_youtube_actor
            case _:
                raise ApifyError(f"Piattaforma non supportata: {platform}")

    def _run_input(self, platform: str, username: str, limit: int) -> dict[str, Any]:
        handle = username.lstrip("@")
        match platform:
            case "instagram":
                return {
                    "directUrls": [f"https://www.instagram.com/{handle}/reels/"],
                    "resultsType": "posts",
                    "resultsLimit": limit,
                }
            case "tiktok":
                return {
                    "profiles": [handle],
                    "resultsPerPage": limit,
                    "shouldDownloadVideos": False,
                    "shouldDownloadCovers": False,
                }
            case "youtube_shorts":
                return {
                    "startUrls": [{"url": f"https://www.youtube.com/@{handle}/shorts"}],
                    "maxResults": limit,
                    "maxResultsShorts": limit,
                }
            case _:
                raise ApifyError(f"Piattaforma non supportata: {platform}")

    # --- API pubblica --------------------------------------------------------

    async def fetch_latest_videos(
        self, platform: str, username: str, limit: int | None = None
    ) -> list[ScrapedVideo]:
        """Ultimi video pubblicati da un creator.

        Solleva `ApifyError` (503) su qualsiasi fallimento: sta al chiamante
        decidere se è fatale. Il job cron, per esempio, la cattura per singolo
        creator e prosegue con gli altri.
        """
        limit = limit or self._settings.apify_results_per_creator
        actor_id = self._actor_id(platform)
        run_input = self._run_input(platform, username, limit)

        items = await self._run_actor(actor_id, run_input, limit)

        videos: list[ScrapedVideo] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            video = _normalize_item(item)
            if video is None or video.video_url in seen:
                continue
            seen.add(video.video_url)
            videos.append(video)

        logger.info(
            "Apify: %s video utilizzabili su %s item per %s/%s",
            len(videos),
            len(items),
            platform,
            username,
        )
        return videos

    async def resolve_video(
        self, video_url: str, platform: str | None = None
    ) -> ScrapedVideo | None:
        """Metadati e URL diretto di un singolo video, se ricavabili.

        Best effort: serve a ottenere `download_url`, `thumbnail_url` e la durata
        prima dell'analisi. Un fallimento non è fatale — si tenta comunque il
        download dell'URL così com'è — quindi qui gli errori vengono assorbiti.
        """
        platform = platform or detect_platform(video_url)
        if platform is None:
            return None

        try:
            actor_id = self._actor_id(platform)
            run_input = self._single_video_input(platform, video_url)
            items = await self._run_actor(actor_id, run_input, 1)
        except ApifyError:
            logger.info("Risoluzione dei metadati non riuscita per %s", video_url)
            return None

        for item in items:
            if isinstance(item, dict) and (video := _normalize_item(item)) is not None:
                return video
        return None

    def _single_video_input(self, platform: str, video_url: str) -> dict[str, Any]:
        match platform:
            case "instagram":
                return {"directUrls": [video_url], "resultsType": "posts", "resultsLimit": 1}
            case "tiktok":
                # Senza questo flag l'actor non restituisce alcun URL scaricabile
                # (né a livello di item né in `videoMeta`): solo `webVideoUrl`,
                # cioè la pagina HTML del post. Verificato chiamando l'actor
                # direttamente — vedi commento su `_authenticate_apify_storage_url`
                # per cosa succede a valle di questo URL una volta ottenuto.
                return {
                    "postURLs": [video_url],
                    "resultsPerPage": 1,
                    "shouldDownloadVideos": True,
                }
            case "youtube_shorts":
                return {"startUrls": [{"url": video_url}], "maxResults": 1}
            case _:
                raise ApifyError(f"Piattaforma non supportata: {platform}")

    # --- Esecuzione ----------------------------------------------------------

    async def _run_actor(
        self, actor_id: str, run_input: dict[str, Any], limit: int
    ) -> list[Any]:
        # `list[Any]` e non `list[dict]`: un dataset Apify contiene JSON
        # arbitrario e nulla garantisce che ogni item sia un oggetto. Dichiararlo
        # come lista di dict renderebbe *morti* i controlli `isinstance` dei
        # chiamanti — che invece sono l'unica cosa che li protegge da un actor
        # che cambia formato fra una release e l'altra.
        """Esegue l'actor con un singolo retry sugli errori transitori."""
        last_error: Exception | None = None

        for attempt in (1, 2):
            try:
                run = await self._client.actor(actor_id).call(
                    run_input=run_input,
                    timeout_secs=self._settings.apify_timeout_seconds,
                    wait_secs=self._settings.apify_timeout_seconds,
                    logger=None,
                )
                if not run:
                    raise ApifyError("L'actor non ha restituito alcuna esecuzione.")

                dataset_id = run.get("defaultDatasetId")
                if not dataset_id:
                    raise ApifyError("L'esecuzione non ha prodotto un dataset.")

                page = await self._client.dataset(dataset_id).list_items(limit=limit)
                return list(page.items)

            except ApifyApiError as exc:
                status = getattr(exc, "status_code", None)
                if status == _RATE_LIMIT_STATUS:
                    logger.warning("Apify in rate limit (429) su actor %s", actor_id)
                if status in _RETRYABLE_STATUS and attempt == 1:
                    last_error = exc
                    await asyncio.sleep(2)
                    continue
                logger.error("Apify: errore API su %s (status=%s)", actor_id, status, exc_info=True)
                raise ApifyError() from exc

            except ApifyClientError as exc:
                if attempt == 1:
                    last_error = exc
                    await asyncio.sleep(2)
                    continue
                logger.error("Apify: errore client su %s", actor_id, exc_info=True)
                raise ApifyError() from exc

            except ApifyError:
                raise

            except Exception as exc:
                logger.error("Apify: fallimento inatteso su %s", actor_id, exc_info=True)
                raise ApifyError() from exc

        raise ApifyError() from last_error


# Allowlist di host esatti. Il confronto è sull'hostname e non su una
# sottostringa dell'URL intero: `https://evil.example/instagram.mp4` contiene
# "instagram." e con il vecchio controllo veniva classificato come Instagram,
# quindi mandato all'actor Instagram con un URL di terzi. Stessa cosa per un
# host come `instagram.com.evil.example`, che a un `in` sembra legittimo.
#
# I sottodomini sono elencati invece di essere accettati con un suffisso: un
# match per suffisso su `.tiktok.com` andrebbe bene, ma su `tiktok.com` senza
# punto iniziale riaprirebbe esattamente il buco che questa allowlist chiude.
_PLATFORM_HOSTS: Final[dict[str, str]] = {
    "instagram.com": "instagram",
    "www.instagram.com": "instagram",
    "m.instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
    "m.tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "vt.tiktok.com": "tiktok",
    "youtube.com": "youtube_shorts",
    "www.youtube.com": "youtube_shorts",
    "m.youtube.com": "youtube_shorts",
    "youtu.be": "youtube_shorts",
}


def detect_platform(video_url: str) -> str | None:
    """Piattaforma dedotta dall'host dell'URL, `None` se non riconosciuta.

    `None` non è un errore: il chiamante ricade sul download diretto dell'URL,
    che resta comunque protetto dai controlli di `media_service`. Serve però a
    non spedire l'URL di un terzo all'actor sbagliato.
    """
    host = urlsplit(video_url.strip()).hostname
    if not host:
        return None
    # Il punto finale di un FQDN (`instagram.com.`) risolve in DNS come l'host
    # senza: senza `rstrip` sarebbe un modo banale di mancare l'allowlist.
    return _PLATFORM_HOSTS.get(host.lower().rstrip("."))


_apify_service: ApifyService | None = None


def get_apify_service() -> ApifyService:
    """Istanza condivisa (usabile come dependency FastAPI)."""
    global _apify_service
    if _apify_service is None:
        _apify_service = ApifyService()
    return _apify_service
