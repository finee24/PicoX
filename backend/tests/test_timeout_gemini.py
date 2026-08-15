"""I limiti temporali dichiarati per Gemini devono arrivare davvero al client.

`gemini_timeout_seconds` esisteva in `config.py` ed era esportato… da nessuna
parte: `grep` sull'intero `app/` ne trovava una sola occorrenza, la definizione.
Il client veniva costruito con la sola `api_key`, quindi una `generate_content`
appesa teneva occupati la richiesta e il worker uvicorn a tempo indefinito.

Una configurazione che dichiara un limite senza applicarlo è peggio di una che
non lo dichiara: chi legge il `render.yaml` crede che il limite ci sia.

Lo stesso vale, al contrario, per i **tentativi**: `google-genai` non ritenta di
suo — il default è `stop_after_attempt(1)` — quindi un limite che non si
dichiara è un retry che non esiste. Misurato il 15 agosto 2026 su
`gemini-flash-latest`: 1 richiesta su 6 tornava `503 "experiencing high demand"`
e faceva fallire l'analisi dell'utente.

E i due retry si moltiplicano, quindi l'ultima sezione verifica che il caso
peggiore resti dentro il TTL del lock: è la garanzia della migration `0003`, che
si perderebbe alzando un timeout senza rifare il conto.
"""

from __future__ import annotations

from google.genai import _api_client
from google.genai import errors as genai_errors

from app.core.config import get_settings
from app.services.gemini_service import _TENTATIVI_SCHEMA, GeminiService


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


# =============================================================================
# Retry sul trasporto
# =============================================================================


def test_i_tentativi_configurati_arrivano_al_client_gemini() -> None:
    settings = get_settings()
    servizio = GeminiService(settings)

    retry = servizio._client._api_client._http_options.retry_options

    # `None` è il caso che ha causato il difetto: significa "non ritentare mai",
    # ed è il default della libreria.
    assert retry is not None
    assert retry.attempts == settings.gemini_retry_attempts


def test_i_tentativi_seguono_la_configurazione_e_non_sono_un_valore_fisso(
    monkeypatch,
) -> None:
    """Se fossero hardcoded, il test sopra passerebbe lo stesso."""
    monkeypatch.setenv("GEMINI_RETRY_ATTEMPTS", "2")
    get_settings.cache_clear()

    servizio = GeminiService(get_settings())

    retry = servizio._client._api_client._http_options.retry_options
    assert retry is not None
    assert retry.attempts == 2


def test_il_503_di_capacita_e_fra_i_codici_ritentati() -> None:
    """Il codice che ha fatto fallire le analisi vere, non un codice a caso.

    Verifica il **predicato** che la libreria costruisce, non la nostra
    intenzione: `HttpRetryOptions` senza `http_status_codes` eredita l'elenco di
    default di `google-genai`, e se una versione futura ne togliesse il 503 la
    nostra configurazione resterebbe identica mentre il comportamento cambia.
    """
    servizio = GeminiService(get_settings())

    argomenti = _api_client.retry_args(
        servizio._client._api_client._http_options.retry_options
    )
    # `retry_if_exception` avvolge il predicato vero, che è la lambda scritta
    # dalla libreria: si interroga quella, non il wrapper, che vorrebbe un
    # `RetryCallState` completo.
    predicato = argomenti["retry"].predicate

    def errore(codice: int) -> genai_errors.APIError:
        classe = (
            genai_errors.ServerError if codice >= 500 else genai_errors.ClientError
        )
        return classe(codice, {"error": {"message": "sovraccarico simulato"}})

    # 503: il sovraccarico misurato. 429: la quota, stesso trattamento.
    assert predicato(errore(503))
    assert predicato(errore(429))
    # 400 è un errore nostro: ritentarlo sarebbe solo spesa.
    assert not predicato(errore(400))


# =============================================================================
# L'invariante che tiene in piedi la garanzia della migration 0003
# =============================================================================


def test_il_caso_peggiore_di_unanalisi_resta_dentro_il_ttl_del_lock() -> None:
    """Il lock non deve poter scadere mentre l'analisi è ancora legittimamente viva.

    Se scadesse, un'altra richiesta lo sottrarrebbe e si tornerebbe a pagare due
    volte Apify e Gemini per lo stesso video — il difetto che `analysis_locks`
    esiste per chiudere.

    I due retry si **moltiplicano**: ogni tentativo sullo schema è una
    `generate_content` che al suo interno può ritentare `gemini_retry_attempts`
    volte. È l'errore di calcolo facile da fare, ed è quello che questo test
    impedisce di fare in silenzio.
    """
    s = get_settings()

    peggiore = (
        s.apify_timeout_seconds
        + s.download_timeout_seconds
        + s.gemini_file_active_timeout_seconds
        + s.gemini_timeout_seconds * s.gemini_retry_attempts * _TENTATIVI_SCHEMA
    )

    assert s.analysis_lock_ttl_seconds >= peggiore, (
        f"Il caso peggiore di un'analisi è {peggiore}s ma il lock scade dopo "
        f"{s.analysis_lock_ttl_seconds}s: una seconda richiesta potrebbe "
        f"sottrarlo a un'analisi in corso e la doppia spesa tornerebbe. "
        f"Alzare analysis_lock_ttl_seconds, oppure abbassare ciò che è cresciuto."
    )


def test_lattesa_di_chi_trova_il_lock_preso_resta_sotto_il_ttl() -> None:
    """Attendere più del TTL non ha senso: a quel punto il lock è sottraibile."""
    s = get_settings()

    assert s.analysis_lock_wait_seconds < s.analysis_lock_ttl_seconds
