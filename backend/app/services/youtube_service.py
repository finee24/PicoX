"""Verifica di un canale tramite YouTube Data API v3.

Perché non Apify anche qui. Lo scraping dei **video** di un canale resta su
Apify — è lì che serve l'URL del media, che l'API ufficiale non dà. Ma per
sapere se un canale esiste l'API ufficiale è gratuita entro quota, risponde in
un round-trip invece che avviando un actor, e non si rompe quando YouTube cambia
il markup della pagina. Pagare uno scraper per un dato che l'API pubblica
regala non avrebbe alcuna contropartita.

**Un canale risolvibile è per definizione pubblico.** YouTube non ha il concetto
di canale privato consultabile solo dai follower: esistono video privati o non
in elenco, ma il canale, se ha un handle che risolve, lo vede chiunque. Per
questo la validazione YouTube non ha un equivalente di `isPrivate` — non è un
dato che abbiamo omesso, è un dato che non esiste.

LA RISORSA SCARSA QUI NON SONO I SOLDI. La Data API concede 10.000 unità al
giorno **per progetto**, e una `channels.list` ne consuma 1. Il tetto è quindi
condiviso fra tutti gli utenti di Picox, ed è una delle ragioni per cui la
validazione ha una quota per utente (migration `0010`): senza, un solo account
potrebbe esaurire la quota YouTube di tutti gli altri.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import YouTubeError

logger = logging.getLogger(__name__)

_CHANNELS_URL: Final = "https://www.googleapis.com/youtube/v3/channels"
_VIDEOS_URL: Final = "https://www.googleapis.com/youtube/v3/videos"
# Le uniche due parti che servono: `snippet` per titolo e avatar, `statistics`
# per gli iscritti. Chiedere `contentDetails` o `brandingSettings` costerebbe
# comunque 1 unità, ma riporterebbe in casa dati che non usiamo — e ogni campo
# che entra è un campo che qualcuno un giorno potrebbe far uscire.
_PARTS: Final = "snippet,statistics"
# Un id di canale inizia sempre così: `UC` seguito da 22 caratteri base64url.
_CHANNEL_ID_PREFIX: Final = "UC"
_CHANNEL_ID_LENGTH: Final = 24


# Id di un video: 11 caratteri base64url. Serve a distinguere un id da un
# segmento di path qualsiasi, non a validarlo contro YouTube.
_VIDEO_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Segmenti che introducono un id di video nel path. `watch` non c'è: lì l'id sta
# nella query (`?v=`), ed è il caso trattato a parte in `video_id_from_url`.
_VIDEO_PATH_PREFIXES: Final = frozenset({"shorts", "embed", "live", "v"})

# `PT1M30S` → 90. Le tre parti sono tutte facoltative e YouTube omette quelle a
# zero, quindi il pattern non può richiederle.
_ISO_DURATION_RE: Final = re.compile(
    r"^P(?:(?P<giorni>\d+)D)?T?(?:(?P<ore>\d+)H)?(?:(?P<minuti>\d+)M)?(?:(?P<secondi>\d+)S)?$"
)


def video_id_from_url(url: str) -> str | None:
    """Id del video contenuto nell'URL, `None` se non ce n'è uno riconoscibile.

    Le forme coperte sono quelle che un utente può incollare:
    `youtube.com/watch?v=ID`, `youtu.be/ID`, `youtube.com/shorts/ID`, più
    `embed` e `live`. Un URL che non combacia non è un errore: il chiamante
    ricade sul percorso con download, che non ha bisogno dell'id.
    """
    parti = urlsplit(url.strip())
    host = (parti.hostname or "").lower().rstrip(".").removeprefix("www.").removeprefix("m.")

    if host == "youtu.be":
        candidato = parti.path.lstrip("/").split("/")[0]
        return candidato if _VIDEO_ID_RE.match(candidato) else None

    if host not in {"youtube.com", "youtube-nocookie.com"}:
        return None

    segmenti = [segmento for segmento in parti.path.split("/") if segmento]
    if segmenti and segmenti[0].lower() in _VIDEO_PATH_PREFIXES and len(segmenti) > 1:
        return segmenti[1] if _VIDEO_ID_RE.match(segmenti[1]) else None

    valori = parse_qs(parti.query).get("v") or []
    if valori and _VIDEO_ID_RE.match(valori[0]):
        return valori[0]
    return None


def _durata_iso_in_secondi(raw: Any) -> float | None:
    """`contentDetails.duration` (ISO-8601) in secondi.

    YouTube restituisce `P0D` per una diretta in corso e per alcuni contenuti
    senza durata definita: in quel caso il risultato è `None`, cioè "non lo so",
    che è diverso da zero e fa scattare il fail-closed a valle.
    """
    if not isinstance(raw, str):
        return None
    trovato = _ISO_DURATION_RE.match(raw.strip())
    if trovato is None:
        return None

    parti = {nome: int(valore or 0) for nome, valore in trovato.groupdict().items()}
    totale = (
        parti["giorni"] * 86400
        + parti["ore"] * 3600
        + parti["minuti"] * 60
        + parti["secondi"]
    )
    return float(totale) if totale > 0 else None


@dataclass(slots=True)
class YouTubeVideo:
    """Metadati di un singolo video, da `videos.list`.

    `duration_seconds` è il campo che conta: sul passthrough non scarichiamo il
    file, quindi è **l'unico** modo di applicare `MAX_VIDEO_DURATION_SECONDS`
    prima di pagare l'inferenza.
    """

    video_id: str
    title: str
    channel_title: str
    duration_seconds: float | None = None
    published_at: datetime | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    thumbnail_url: str | None = None


@dataclass(slots=True)
class YouTubeChannel:
    """Canale trovato, ridotto ai campi che la preview mostra."""

    channel_id: str
    handle: str
    title: str
    avatar_url: str | None = None
    subscriber_count: int = 0


def _is_channel_id(identifier: str) -> bool:
    return (
        len(identifier) == _CHANNEL_ID_LENGTH
        and identifier.startswith(_CHANNEL_ID_PREFIX)
        and identifier.replace("-", "").replace("_", "").isalnum()
    )


def _sub_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Sotto-oggetto della risposta, `{}` se assente o di forma inattesa.

    L'API dichiara `snippet` e `statistics` sempre presenti quando li si chiede
    in `part`, ma una risposta è comunque JSON arbitrario finché non la si
    controlla: qui il campo mancante diventa un profilo con meno dati, non un
    500.
    """
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _best_thumbnail(snippet: dict[str, Any]) -> str | None:
    """L'avatar più grande disponibile.

    Le tre taglie non sono sempre tutte presenti: si scende dalla migliore alla
    peggiore invece di dare per scontata `high`, che su un canale nuovo può
    mancare.
    """
    thumbnails = snippet.get("thumbnails")
    if not isinstance(thumbnails, dict):
        return None
    for size in ("high", "medium", "default"):
        entry = thumbnails.get(size)
        if isinstance(entry, dict):
            url = entry.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def _contatore(raw: Any) -> int | None:
    """Un contatore della Data API, che li serializza **come stringhe**.

    `None` quando il campo manca: su un video con i like nascosti l'API omette
    `likeCount`, e zero sarebbe un'affermazione che non abbiamo.
    """
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def _come_datetime(raw: Any) -> datetime | None:
    """`publishedAt` (RFC 3339 con la `Z`) come datetime."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _subscriber_count(statistics: dict[str, Any]) -> int:
    """Iscritti, `0` se il canale li nasconde.

    L'API li restituisce come **stringa** (`"12300"`), e con
    `hiddenSubscriberCount: true` non li restituisce affatto. Zero è quindi
    "non pubblicati" oltre che "nessuno", ed è la stessa convenzione usata per
    i follower nascosti su Instagram: la preview non ha modo di distinguerli e
    non deve far finta di poterlo fare.
    """
    return _contatore(statistics.get("subscriberCount")) or 0


class YouTubeService:
    """Client minimale della Data API, limitato a `channels.list`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def is_configured(self) -> bool:
        """`False` quando `YOUTUBE_API_KEY` non è impostata.

        Facoltativa di proposito: senza chiave, Instagram e TikTok continuano a
        validare normalmente e solo YouTube risponde 503. Un'installazione a cui
        YouTube non interessa non deve procurarsi una credenziale per far
        partire il backend.
        """
        key = self._settings.youtube_api_key
        return key is not None and bool(key.get_secret_value().strip())

    async def fetch_channel(self, identifier: str) -> YouTubeChannel | None:
        """Canale corrispondente all'handle (o all'id), `None` se non esiste.

        Come per `ApifyService.fetch_profile`, `None` e l'eccezione dicono due
        cose diverse e non vanno confuse: `None` è l'API che risponde "non
        c'è", `YouTubeError` è l'API che non risponde. Trattare un guasto come
        "canale inesistente" direbbe all'utente che il suo canale non esiste
        ogni volta che la quota è esaurita.
        """
        if not self.is_configured:
            # Il motivo resta nei log, dove serve a chi configura il deploy: nel
            # corpo della risposta sarebbe una descrizione della nostra
            # infrastruttura restituita a chiunque chiami l'endpoint.
            logger.error(
                "Validazione YouTube richiesta ma YOUTUBE_API_KEY non è configurata."
            )
            raise YouTubeError()

        handle = identifier.lstrip("@")
        params: dict[str, str] = {
            "part": _PARTS,
            "key": self._settings.youtube_api_key.get_secret_value(),  # type: ignore[union-attr]
        }
        # Un handle e un id di canale sono parametri diversi della stessa
        # chiamata: passare un id a `forHandle` non trova nulla, ed è la forma
        # che arriva dagli URL `youtube.com/channel/UC…`.
        if _is_channel_id(handle):
            params["id"] = handle
        else:
            params["forHandle"] = f"@{handle}"

        payload = await self._get(params)

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            logger.info("YouTube: nessun canale per @%s", handle)
            return None

        item = items[0]
        if not isinstance(item, dict):
            return None

        snippet = _sub_object(item, "snippet")
        statistics = _sub_object(item, "statistics")

        custom_url = snippet.get("customUrl")
        return YouTubeChannel(
            channel_id=str(item.get("id") or handle),
            # `customUrl` è l'handle canonico secondo YouTube: se l'utente ha
            # scritto una variante di maiuscole, è questo il valore giusto da
            # mostrare e da salvare.
            handle=(custom_url.lstrip("@") if isinstance(custom_url, str) else handle),
            title=str(snippet.get("title") or handle),
            avatar_url=_best_thumbnail(snippet),
            subscriber_count=_subscriber_count(statistics),
        )

    async def fetch_video(self, video_id: str) -> YouTubeVideo | None:
        """Metadati di un singolo video, `None` se non esiste o non è pubblico.

        `videos.list` risponde con `items` vuoto anche per un video privato o
        rimosso: dal nostro punto di vista è lo stesso caso — non lo possiamo
        analizzare — e il chiamante ricade sul percorso con download, che almeno
        ci prova.

        Una unità di quota, come `channels.list`.
        """
        if not self.is_configured:
            logger.info(
                "Metadati YouTube non disponibili: YOUTUBE_API_KEY non è configurata."
            )
            return None

        payload = await self._get(
            {
                # `contentDetails` porta la durata, che è il motivo principale
                # per cui questa chiamata esiste nel flusso principale.
                "part": "snippet,contentDetails,statistics",
                "id": video_id,
                "key": self._settings.youtube_api_key.get_secret_value(),  # type: ignore[union-attr]
            }
        )

        items = payload.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            logger.info("YouTube: nessun video pubblico con id %s", video_id)
            return None

        item = items[0]
        snippet = _sub_object(item, "snippet")
        content = _sub_object(item, "contentDetails")
        statistics = _sub_object(item, "statistics")

        return YouTubeVideo(
            video_id=str(item.get("id") or video_id),
            title=str(snippet.get("title") or ""),
            channel_title=str(snippet.get("channelTitle") or ""),
            duration_seconds=_durata_iso_in_secondi(content.get("duration")),
            published_at=_come_datetime(snippet.get("publishedAt")),
            view_count=_contatore(statistics.get("viewCount")),
            like_count=_contatore(statistics.get("likeCount")),
            comment_count=_contatore(statistics.get("commentCount")),
            thumbnail_url=_best_thumbnail(snippet),
        )

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        """Esegue la chiamata, traducendo ogni fallimento in `YouTubeError`.

        Nessun dettaglio dell'errore raggiunge il client: il corpo di un errore
        Google contiene il motivo (`quotaExceeded`, `keyInvalid`) e a volte
        frammenti della richiesta, **chiave compresa**. Resta nei log, troncato.
        """
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.youtube_api_timeout_seconds, connect=10.0),
                headers={"User-Agent": "PicoxBot/1.0"},
            ) as client:
                response = await client.get(_CHANNELS_URL, params=params)
        except httpx.HTTPError as exc:
            logger.error("YouTube Data API irraggiungibile: %s", type(exc).__name__)
            raise YouTubeError() from exc

        if response.status_code != httpx.codes.OK:
            # `response.text` e non il json: un errore può non essere JSON
            # affatto (pagina di errore di un proxy), e il troncamento vale in
            # entrambi i casi.
            logger.error(
                "YouTube Data API: status %s — %s",
                response.status_code,
                response.text[:300],
            )
            raise YouTubeError()

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("YouTube Data API: risposta non JSON.")
            raise YouTubeError() from exc

        if not isinstance(payload, dict):
            logger.error("YouTube Data API: risposta di forma inattesa.")
            raise YouTubeError()
        return payload


_youtube_service: YouTubeService | None = None


def get_youtube_service() -> YouTubeService:
    """Istanza condivisa (usabile come dependency FastAPI)."""
    global _youtube_service
    if _youtube_service is None:
        _youtube_service = YouTubeService()
    return _youtube_service
