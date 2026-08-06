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


def _guess_mime_type(url: str, content_type: str | None) -> str:
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base.startswith("video/"):
            return base
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
    except (OSError, asyncio.TimeoutError):
        logger.warning("ffprobe non riuscito sul file scaricato.", exc_info=True)
        return None

    if process.returncode != 0:
        return None
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None


def _enforce_duration(duration: float | None, settings: Settings) -> None:
    if duration is None:
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
    _enforce_duration(known_duration_seconds, settings)

    fd, temp_path = tempfile.mkstemp(prefix="picox_", suffix=".mp4")
    os.close(fd)

    try:
        downloaded = await _stream_to_file(url, temp_path, settings)

        duration = known_duration_seconds
        if duration is None:
            duration = await probe_duration_seconds(temp_path)
        _enforce_duration(duration, settings)

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


async def _open_validated_stream(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Apre lo stream seguendo i redirect **uno alla volta**, validandoli tutti.

    `follow_redirects=True` di httpx salterebbe il controllo su ogni hop
    successivo al primo, che è esattamente dove un attaccante lo aggirerebbe.
    """
    current = url

    for _ in range(_MAX_REDIRECTS + 1):
        await _assert_public_target(current)

        response = await client.send(client.build_request("GET", current), stream=True)

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
            response = await _open_validated_stream(client, url)
            try:
                # Se la CDN dichiara la dimensione, si rifiuta prima di scaricare.
                declared = response.headers.get("content-length")
                if declared and declared.isdigit():
                    _enforce_size(int(declared), settings)

                mime_type = _guess_mime_type(
                    str(response.url), response.headers.get("content-type")
                )

                with open(dest_path, "wb") as handle:
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
        logger.warning(
            "Download del video fallito: status %s", exc.response.status_code, exc_info=True
        )
        raise ExternalServiceError(
            "Impossibile scaricare il video: la sorgente non è raggiungibile.",
            service="media",
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("Download del video fallito: %s", type(exc).__name__, exc_info=True)
        raise ExternalServiceError(
            "Impossibile scaricare il video: la sorgente non è raggiungibile.",
            service="media",
        ) from exc

    if written == 0:
        raise ExternalServiceError("Il video scaricato è vuoto.", service="media")

    return DownloadedVideo(
        path=dest_path, size_bytes=written, mime_type=mime_type, duration_seconds=None
    )
