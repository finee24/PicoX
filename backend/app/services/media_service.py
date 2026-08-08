"""Normalizzazione degli URL e download del video in un file temporaneo.

Due responsabilità, entrambe a monte dell'inferenza:

1. **Normalizzare `video_url`.** È la chiave naturale del vincolo
   `UNIQUE (user_id, video_url)`. Senza normalizzazione lo stesso Reel con un
   `?igshid=` diverso supera il vincolo e viene pagato due volte.

2. **Scaricare il video entro i limiti.** Il limite di dimensione è applicato
   *durante* lo streaming, non dopo: un file da 2 GB viene interrotto ai 200 MB
   configurati invece di essere scaricato per intero e poi scartato.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import mimetypes
import os
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.exceptions import (
    ExternalServiceError,
    PicoxValidationError,
    VideoTooLargeError,
    VideoTooLongError,
)

logger = logging.getLogger(__name__)

# Parametri di tracciamento: non identificano il video, solo la provenienza del
# click. Rimuoverli è ciò che rende stabile la chiave di cache.
_TRACKING_PARAMS = frozenset(
    {
        "igshid",
        "igsh",
        "fbclid",
        "gclid",
        "mibextid",
        "si",
        "feature",
        "app",
        "is_from_webapp",
        "sender_device",
        "web_id",
        "_r",
        "_t",
        "share_app_id",
        "share_link_id",
        "refer",
        "referer",
        "source",
    }
)

_HOST_ALIASES = {
    "www.instagram.com": "instagram.com",
    "m.instagram.com": "instagram.com",
    "www.tiktok.com": "tiktok.com",
    "m.tiktok.com": "tiktok.com",
    "vm.tiktok.com": "tiktok.com",
    "www.youtube.com": "youtube.com",
    "m.youtube.com": "youtube.com",
    "youtu.be": "youtube.com",
}

# Le famiglie di parametri di tracciamento si riconoscono dal prefisso: `utm_*`
# di Google Analytics, `mc_*` di Mailchimp. Elencarli uno per uno significherebbe
# inseguirli, e ogni parametro sfuggito è un'analisi pagata due volte.
_TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "ref_")

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_VIDEO_MIME_FALLBACK = "video/mp4"

_MAX_REDIRECTS = 5

# Host del key value store di Apify. È l'unico verso cui il downloader invia una
# credenziale: vedi `_per_hop_headers`.
_APIFY_STORAGE_HOST = "api.apify.com"


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def normalize_video_url(raw_url: str) -> str:
    """Forma canonica dell'URL, usata come chiave di cache e di dedup.

    Rimuove i parametri di tracciamento, uniforma host e schema, elimina il
    frammento e lo slash finale. I parametri superstiti restano ordinati, così
    che `?a=1&b=2` e `?b=2&a=1` collassino sulla stessa chiave.
    """
    url = raw_url.strip()
    if not url:
        raise PicoxValidationError("URL del video mancante.")

    parts = urlsplit(url)

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise PicoxValidationError("Sono ammessi solo URL http o https.")
    if not parts.netloc:
        raise PicoxValidationError("URL del video non valido.")

    host = parts.netloc.lower()
    if "@" in host:  # credenziali nell'URL: non le propaghiamo
        host = host.rsplit("@", 1)[-1]
    host = host.removeprefix("www.") if host not in _HOST_ALIASES else host
    host = _HOST_ALIASES.get(host, host)

    # `youtu.be/<id>` -> `youtube.com/shorts/<id>` non è una riscrittura sicura
    # in generale, quindi ci si limita a normalizzare l'host e a tenere il path.
    path = parts.path.rstrip("/") or "/"

    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not _is_tracking_param(k)
    )
    query = urlencode(kept)

    return urlunsplit(("https", host, path, query, ""))


@dataclass(slots=True)
class DownloadedVideo:
    """Video scaricato in locale, pronto per l'upload su Gemini."""

    path: str
    size_bytes: int
    mime_type: str
    duration_seconds: float | None


# Content-Type che una CDN video non serve mai per un file vero: se compaiono
# qui, l'URL non sta restituendo un video ma una pagina di errore, un login
# wall o un JSON — capita tipicamente quando l'URL "diretto" in realtà punta
# ancora alla pagina del post (risoluzione dei metadati fallita a monte).
# Senza questo controllo il contenuto verrebbe comunque incollato come
# `video/mp4` (fallback più sotto) e mandato a Gemini, che lo rifiuta con un
# `FileState.FAILED` poco leggibile — 503 uguale, ma senza dire perché.
_DEFINITELY_NOT_VIDEO: Final = frozenset(
    {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}
)


def _guess_mime_type(url: str, content_type: str | None) -> str:
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base.startswith("video/"):
            return base
        if base in _DEFINITELY_NOT_VIDEO:
            raise ExternalServiceError(
                "La sorgente non ha restituito un video valido. "
                "Il link potrebbe richiedere l'accesso o non essere più disponibile.",
                service="media",
            )
        # Le CDN servono spesso i video come application/octet-stream.
        if base not in {"", "application/octet-stream", "binary/octet-stream"}:
            logger.debug("Content-Type inatteso per un video: %s", base)

    guessed, _ = mimetypes.guess_type(urlsplit(url).path)
    if guessed and guessed.startswith("video/"):
        return guessed
    return _VIDEO_MIME_FALLBACK


async def probe_duration_seconds(path: str) -> float | None:
    """Durata del video via `ffprobe`, se disponibile nell'immagine.

    Restituisce `None` quando `ffprobe` non c'è o non riesce a leggere il file:
    la guard sulla durata è un controllo aggiuntivo, non deve impedire l'analisi
    se lo strumento manca. Il limite di dimensione resta comunque applicato.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        logger.warning(
            "ffprobe non disponibile: il limite di durata non può essere verificato "
            "per questo video (resta attivo quello di dimensione)."
        )
        return None

    try:
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except (TimeoutError, OSError):
        logger.warning("ffprobe non riuscito sul file scaricato.", exc_info=True)
        return None

    if process.returncode != 0:
        return None
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None


def _enforce_duration(
    duration: float | None, settings: Settings, *, verificabile: bool = True
) -> None:
    """Applica il limite di durata.

    Con `verificabile=False` una durata sconosciuta diventa un rifiuto, invece di
    saltare il controllo. È il comportamento voluto **dopo** il download, quando
    ogni fonte di durata è stata tentata: se a quel punto non la conosciamo, il
    limite configurato non è applicabile, e lasciar passare il video vorrebbe
    dire che `MAX_VIDEO_DURATION_SECONDS` non significa nulla.

    Un `None` silenzioso è esattamente come il limite si era già disattivato una
    volta: `ffprobe` assente sul runtime Python di Render, `probe_duration_
    seconds` che restituisce `None`, e nessun segnale da nessuna parte. La
    configurazione dichiarava un limite che il runtime non applicava.
    """
    if duration is None:
        if not verificabile:
            raise PicoxValidationError(
                "Non è stato possibile determinare la durata del video, quindi il "
                "limite configurato non può essere applicato. Il video è stato "
                "rifiutato per prudenza."
            )
        return
    if duration > settings.max_video_duration_seconds:
        raise VideoTooLongError(
            f"Il video dura {duration:.0f} secondi e supera il limite di "
            f"{settings.max_video_duration_seconds} secondi."
        )


def _enforce_size(size_bytes: int, settings: Settings) -> None:
    if size_bytes > settings.max_video_bytes:
        raise VideoTooLargeError(
            f"Il video pesa {size_bytes / 1024 / 1024:.0f} MB e supera il limite di "
            f"{settings.max_video_mb} MB."
        )


@asynccontextmanager
async def download_to_temp(
    url: str,
    settings: Settings,
    *,
    known_duration_seconds: float | None = None,
) -> AsyncIterator[DownloadedVideo]:
    """Scarica il video in un file temporaneo, garantendone la cancellazione.

    Il `finally` cancella il file in ogni caso: successo, eccezione durante il
    download, eccezione sollevata dal blocco `async with` del chiamante. Un
    fallimento della cancellazione viene loggato ma non propagato, per non
    trasformare un'analisi riuscita in un 500.
    """
    # Pre-controllo con i metadati di Apify, che sono best effort e spessissimo
    # assenti. Qui `None` deve restare permissivo: renderlo fail-closed
    # rifiuterebbe ogni video che Apify non è riuscito a descrivere, prima
    # ancora di scaricarlo. Il controllo che non ammette incertezza è quello
    # *dopo* il download, dove la durata si può misurare.
    _enforce_duration(known_duration_seconds, settings)

    fd, temp_path = tempfile.mkstemp(prefix="picox_", suffix=".mp4")
    os.close(fd)

    try:
        downloaded = await _stream_to_file(url, temp_path, settings)

        duration = known_duration_seconds
        if duration is None:
            duration = await probe_duration_seconds(temp_path)
        # Il file è sul disco: ogni fonte di durata è stata tentata. Se è ancora
        # ignota, si rifiuta invece di ignorare il limite.
        _enforce_duration(duration, settings, verificabile=False)

        yield DownloadedVideo(
            path=temp_path,
            size_bytes=downloaded.size_bytes,
            mime_type=downloaded.mime_type,
            duration_seconds=duration,
        )
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Impossibile rimuovere il file temporaneo %s", temp_path, exc_info=True)


async def _assert_public_target(url: str) -> None:
    """Rifiuta gli URL che puntano alla rete interna.

    `video_url` arriva dall'utente, quindi senza questo controllo l'endpoint di
    analisi è un SSRF: l'attaccante fa scaricare al backend
    `http://169.254.169.254/…` (metadata dell'istanza) o un servizio su
    `127.0.0.1`, usando la differenza fra gli errori come oracolo per mappare la
    rete — e nel caso peggiore facendo finire il contenuto interno su Gemini.

    Il controllo è sull'**IP risolto**, non sul nome: un host pubblico può avere
    un record A che punta a `127.0.0.1`. Va applicato a ogni hop di redirect,
    perché è lì che passa l'attacco più comune (host innocuo che risponde 302).

    Resta fuori portata il DNS rebinding puro (risposta DNS che cambia fra il
    controllo e la connessione di httpx): chiuderlo richiede di forzare la
    connessione sull'IP già validato. Il rischio residuo è molto più stretto
    dell'SSRF diretto che questo controllo elimina.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise PicoxValidationError("Sono ammessi solo URL http o https.")

    host = parts.hostname
    if not host:
        raise PicoxValidationError("URL del video non valido.")

    try:
        infos = await run_in_threadpool(
            socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ExternalServiceError(
            "Impossibile risolvere l'host del video.", service="media"
        ) from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise ExternalServiceError(
            "Impossibile risolvere l'host del video.", service="media"
        )

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise PicoxValidationError("URL del video non valido.") from None
        if not ip.is_global or ip.is_multicast:
            logger.warning(
                "Download rifiutato: %s risolve su un indirizzo non pubblico.", host
            )
            raise PicoxValidationError(
                "L'URL indicato non punta a un video pubblico raggiungibile."
            )


def _per_hop_headers(url: str, settings: Settings) -> dict[str, str]:
    """Header di autenticazione per *questo* hop, ricalcolati a ogni redirect.

    Con `shouldDownloadVideos: true` gli actor restituiscono un record nel key
    value store di Apify, che senza credenziale risponde `403`. La credenziale
    viaggia in header e non in query string: un URL finisce nel messaggio di
    ogni eccezione httpx, nei log dei proxy intermedi e nel `Referer`, mentre un
    header no.

    Il calcolo è per hop, non una volta sola, proprio perché un redirect può
    portare su un host diverso: lì il token non deve seguire. È il motivo per
    cui questi header non stanno sul client httpx, che li applicherebbe a
    tutte le richieste della sessione.
    """
    if urlsplit(url).hostname != _APIFY_STORAGE_HOST:
        return {}
    return {"Authorization": f"Bearer {settings.apify_api_token.get_secret_value()}"}


async def _open_validated_stream(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> httpx.Response:
    """Apre lo stream seguendo i redirect **uno alla volta**, validandoli tutti.

    `follow_redirects=True` di httpx salterebbe il controllo su ogni hop
    successivo al primo, che è esattamente dove un attaccante lo aggirerebbe.
    """
    current = url

    for _ in range(_MAX_REDIRECTS + 1):
        await _assert_public_target(current)

        response = await client.send(
            client.build_request("GET", current, headers=_per_hop_headers(current, settings)),
            stream=True,
        )

        if response.is_redirect and response.next_request is not None:
            next_url = str(response.next_request.url)
            await response.aclose()
            current = next_url
            continue

        try:
            response.raise_for_status()
        except BaseException:
            await response.aclose()
            raise
        return response

    raise ExternalServiceError(
        "Troppi redirect nel download del video.", service="media"
    )


async def _stream_to_file(url: str, dest_path: str, settings: Settings) -> DownloadedVideo:
    """Scarica in streaming interrompendosi appena si supera il limite."""
    max_bytes = settings.max_video_bytes
    written = 0

    try:
        async with httpx.AsyncClient(
            # I redirect li seguiamo a mano, validando ogni hop.
            follow_redirects=False,
            timeout=httpx.Timeout(settings.download_timeout_seconds, connect=15.0),
            headers={"User-Agent": "PicoxBot/1.0"},
        ) as client:
            response = await _open_validated_stream(client, url, settings)
            try:
                # Se la CDN dichiara la dimensione, si rifiuta prima di scaricare.
                declared = response.headers.get("content-length")
                if declared and declared.isdigit():
                    _enforce_size(int(declared), settings)

                mime_type = _guess_mime_type(
                    str(response.url), response.headers.get("content-type")
                )

                # La scrittura bloccante in funzione async (ASYNC230) è una
                # scelta: ogni `write` è di 256 KB verso la page cache, decine
                # di microsecondi, mentre l'attesa vera è sul chunk successivo
                # dalla rete. Spostarla in un threadpool aggiungerebbe un hop
                # per chunk — stesso costo, più complessità, nessun guadagno.
                with open(dest_path, "wb") as handle:  # noqa: ASYNC230
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                        written += len(chunk)
                        if written > max_bytes:
                            # Interrompe il trasferimento: non ha senso pagare
                            # banda per un file che verrà comunque rifiutato.
                            raise VideoTooLargeError(
                                f"Il video supera il limite di {settings.max_video_mb} MB."
                            )
                        handle.write(chunk)
            finally:
                await response.aclose()

    except httpx.HTTPStatusError as exc:
        # Niente `exc_info`: il messaggio di `HTTPStatusError` contiene l'URL
        # completo, e un URL di download può portare con sé una credenziale in
        # query string. Lo status da solo dice già tutto quel che serve per
        # diagnosticare, e il traceback non aggiungerebbe nulla di azionabile.
        logger.warning(
            "Download del video fallito: status %s", exc.response.status_code
        )
        raise ExternalServiceError(
            "Impossibile scaricare il video: la sorgente non è raggiungibile.",
            service="media",
        ) from exc
    except httpx.HTTPError as exc:
        # Stessa ragione: `str(exc)` di molte eccezioni httpx include l'URL.
        logger.warning("Download del video fallito: %s", type(exc).__name__)
        raise ExternalServiceError(
            "Impossibile scaricare il video: la sorgente non è raggiungibile.",
            service="media",
        ) from exc

    if written == 0:
        raise ExternalServiceError("Il video scaricato è vuoto.", service="media")

    return DownloadedVideo(
        path=dest_path, size_bytes=written, mime_type=mime_type, duration_seconds=None
    )
