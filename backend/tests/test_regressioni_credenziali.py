"""Regressioni: nessuna credenziale deve finire in un URL o in un log.

Nasce da un incidente reale su questo progetto — la service_role key di Supabase
trovata in chiaro in `backend.log` — e dal fatto che il token Apify percorreva la
stessa strada: dentro un URL, e da lì nel messaggio di ogni eccezione httpx.

Questi test coprono `media_service`, che fino a ora non era esercitato da alcun
test: la fixture `downloads` di `conftest.py` sostituisce `download_to_temp` in
tutta la suite, quindi il modulo vero non veniva mai eseguito.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError
from app.services import media_service
from app.services.apify_service import _normalize_item

TOKEN_FINTO = "apify_api_TOKENxFINTOxPERxILxTESTx1234567890"  # scan-secrets:ok


# =============================================================================
# Il token viaggia in header, e solo verso l'host di Apify
# =============================================================================


def test_il_token_apify_va_in_header_solo_verso_lo_storage_apify() -> None:
    settings = get_settings()

    headers = media_service._per_hop_headers(
        "https://api.apify.com/v2/key-value-stores/abc/records/video", settings
    )
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.tiktokcdn.com/video.mp4",
        "https://evil.example/video.mp4",
        # Il caso che conta: un redirect che porta fuori da Apify. Se il token
        # fosse sul client httpx invece che per-hop, qui lo seguirebbe.
        "https://api.apify.com.evil.example/records/video",
        "https://apify.com/v2/key-value-stores/abc",
    ],
)
def test_il_token_apify_non_segue_su_altri_host(url: str) -> None:
    assert media_service._per_hop_headers(url, get_settings()) == {}


def test_l_url_di_download_non_contiene_mai_il_token() -> None:
    """Prima della correzione il token veniva appeso in query string."""
    video = _normalize_item(
        {
            "webVideoUrl": "https://www.tiktok.com/@tizio/video/123",
            "mediaUrl": "https://api.apify.com/v2/key-value-stores/abc/records/video",
        }
    )

    assert video is not None
    assert video.download_url is not None
    assert "token" not in video.download_url
    assert "apify_api_" not in video.download_url


# =============================================================================
# Il fallimento del download non stampa l'URL
# =============================================================================


async def test_un_download_fallito_non_logga_l_url_con_la_credenziale(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    """Il caso dell'incidente: `exc_info=True` serializzava l'URL completo.

    `HTTPStatusError` porta l'URL nel proprio messaggio, quindi il traceback lo
    esponeva per intero — token compreso.
    """
    url_con_segreto = (
        f"https://api.apify.com/v2/key-value-stores/abc/records/x?token={TOKEN_FINTO}"
    )

    async def _esplode(*_args: object, **_kwargs: object) -> httpx.Response:
        richiesta = httpx.Request("GET", url_con_segreto)
        raise httpx.HTTPStatusError(
            f"Client error '403 Forbidden' for url '{url_con_segreto}'",
            request=richiesta,
            response=httpx.Response(403, request=richiesta),
        )

    monkeypatch.setattr(media_service, "_open_validated_stream", _esplode)

    destinazione = tmp_path / "video.mp4"
    with caplog.at_level(logging.DEBUG), pytest.raises(ExternalServiceError):
        await media_service._stream_to_file(
            url_con_segreto, str(destinazione), get_settings()
        )

    registrato = "\n".join(
        record.getMessage() + (record.exc_text or "") for record in caplog.records
    )
    assert TOKEN_FINTO not in registrato
    assert "token=" not in registrato
    assert "api.apify.com" not in registrato
    # Lo status resta: serve a diagnosticare, e non è un segreto.
    assert "403" in registrato


async def test_un_errore_di_rete_non_logga_l_url_con_la_credenziale(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    """Stessa cosa sul ramo `httpx.HTTPError`: molte sue sottoclassi citano l'URL."""
    url_con_segreto = (
        f"https://api.apify.com/v2/key-value-stores/abc/records/x?token={TOKEN_FINTO}"
    )

    async def _esplode(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError(
            f"[Errno 111] Connection refused for url '{url_con_segreto}'"
        )

    monkeypatch.setattr(media_service, "_open_validated_stream", _esplode)

    destinazione = tmp_path / "video.mp4"
    with caplog.at_level(logging.DEBUG), pytest.raises(ExternalServiceError):
        await media_service._stream_to_file(
            url_con_segreto, str(destinazione), get_settings()
        )

    registrato = "\n".join(
        record.getMessage() + (record.exc_text or "") for record in caplog.records
    )
    assert TOKEN_FINTO not in registrato
    assert "token=" not in registrato
