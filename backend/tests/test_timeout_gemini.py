"""Il timeout dichiarato per Gemini deve arrivare davvero al client.

`gemini_timeout_seconds` esisteva in `config.py` ed era esportato… da nessuna
parte: `grep` sull'intero `app/` ne trovava una sola occorrenza, la definizione.
Il client veniva costruito con la sola `api_key`, quindi una `generate_content`
appesa teneva occupati la richiesta e il worker uvicorn a tempo indefinito.

Una configurazione che dichiara un limite senza applicarlo è peggio di una che
non lo dichiara: chi legge il `render.yaml` crede che il limite ci sia.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.services.gemini_service import GeminiService


def test_il_timeout_configurato_arriva_al_client_gemini() -> None:
    settings = get_settings()
    servizio = GeminiService(settings)

    http_options = servizio._client._api_client._http_options

    # `HttpOptions.timeout` è in millisecondi, la configurazione in secondi:
    # è esattamente la conversione in cui è facile sbagliare di 1000.
    assert http_options.timeout == settings.gemini_timeout_seconds * 1000


def test_il_timeout_segue_la_configurazione_e_non_e_un_valore_fisso(
    monkeypatch,
) -> None:
    """Se fosse hardcoded, il test sopra passerebbe lo stesso."""
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "42")
    get_settings.cache_clear()

    servizio = GeminiService(get_settings())

    assert servizio._client._api_client._http_options.timeout == 42_000
