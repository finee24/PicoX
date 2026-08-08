"""Hardening: autenticazione, forma degli errori, CORS, logging correlato.

Quattro proprietà che devono valere per *tutta* l'API, non per un endpoint in
particolare — motivo per cui alcuni test enumerano le route invece di elencarle
a mano: una route nuova aggiunta senza autenticazione deve far fallire la suite,
non passare inosservata.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.observability import (
    JsonLogFormatter,
    RequestContextFilter,
    request_id_var,
    sanitize_incoming_request_id,
    user_id_var,
)
from tests.conftest import (
    TEST_CRON_SECRET,
    TEST_FRONTEND_URL,
    TEST_JWT_SECRET,
    USER_ID,
)
from tests.fake_supabase import FakeStore, make_jwt

PLACEHOLDER_ID = "33333333-3333-4333-8333-333333333333"


def _concrete_path(path: str) -> str:
    return path.replace("{creator_id}", PLACEHOLDER_ID)


def _protected_routes(app: Any) -> list[tuple[str, str]]:
    """Ogni route sotto `/api/v1` che non sia quella del cron (autenticata a parte).

    L'elenco arriva dallo schema OpenAPI e non da `app.routes`: quest'ultimo non
    appiattisce i router inclusi (compaiono come oggetti opachi), quindi
    iterarlo restituirebbe una lista vuota — un test che passa senza verificare
    niente. Lo schema è invece API pubblica e vede tutte le operazioni.
    """
    routes: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/v1") or path.startswith("/api/v1/cron"):
            continue
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS", "PARAMETERS"}:
                continue
            routes.append((method.upper(), _concrete_path(path)))
    return sorted(routes)


# =============================================================================
# 401 su tutto ciò che è protetto
# =============================================================================


def test_ogni_endpoint_protetto_rifiuta_le_richieste_senza_jwt(
    client: TestClient, app: Any
) -> None:
    routes = _protected_routes(app)
    assert routes, "Nessuna route protetta trovata: il test non sta verificando nulla."

    non_conformi = []
    for method, path in routes:
        response = client.request(method, path, json={})
        if response.status_code != 401:
            non_conformi.append((method, path, response.status_code))

    assert not non_conformi, f"Route raggiungibili senza JWT: {non_conformi}"


@pytest.mark.parametrize(
    "header",
    [
        pytest.param({}, id="header-assente"),
        pytest.param({"Authorization": "Bearer non-un-jwt"}, id="token-malformato"),
        pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, id="schema-sbagliato"),
        pytest.param({"Authorization": "Bearer "}, id="token-vuoto"),
    ],
)
def test_credenziali_non_valide_producono_401(
    client: TestClient, header: dict[str, str]
) -> None:
    response = client.get("/api/v1/insights", headers=header)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    # Il client deve poter distinguere "riautenticati" da un 401 generico.
    assert response.headers.get("www-authenticate") == "Bearer"


def test_jwt_firmato_con_un_altro_segreto_viene_rifiutato(client: TestClient) -> None:
    forged = make_jwt(USER_ID, "un-segreto-che-non-e-quello-del-progetto-0123")

    response = client.get("/api/v1/insights", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


def test_jwt_scaduto_viene_rifiutato(client: TestClient) -> None:
    expired = make_jwt(USER_ID, TEST_JWT_SECRET, expires_in=-60)

    response = client.get("/api/v1/insights", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401


def test_il_motivo_del_401_non_viene_rivelato(client: TestClient) -> None:
    """Scaduto, firma errata e malformato devono essere indistinguibili.

    Un messaggio diverso per caso è un oracolo: dice a chi sonda l'API se il
    token era stato emesso da questo progetto.
    """
    messaggi = {
        client.get(
            "/api/v1/insights", headers={"Authorization": f"Bearer {token}"}
        ).json()["error"]["message"]
        for token in (
            make_jwt(USER_ID, TEST_JWT_SECRET, expires_in=-60),
            make_jwt(USER_ID, "un-altro-segreto-lungo-abbastanza-0123456789"),
            "palesemente-non-un-jwt",
        )
    }

    assert len(messaggi) == 1, f"Messaggi distinguibili fra loro: {messaggi}"


# =============================================================================
# Cron: segreto condiviso
# =============================================================================


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="header-assente"),
        pytest.param({"X-CRON-SECRET": ""}, id="vuoto"),
        pytest.param({"X-CRON-SECRET": "sbagliato"}, id="errato"),
        pytest.param({"X-CRON-SECRET": TEST_CRON_SECRET + "x"}, id="prefisso-corretto"),
    ],
)
def test_cron_rifiuta_i_segreti_non_validi(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.post("/api/v1/cron/check-updates", headers=headers)

    assert response.status_code == 401


def test_cron_accetta_il_segreto_corretto(
    client: TestClient, cron_headers: dict[str, str]
) -> None:
    response = client.post("/api/v1/cron/check-updates", headers=cron_headers)

    assert response.status_code == 200


def test_un_jwt_utente_non_apre_il_cron(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Le due autenticazioni sono separate: un utente non può forzare il giro."""
    response = client.post("/api/v1/cron/check-updates", headers=auth_headers)

    assert response.status_code == 401


# =============================================================================
# Nessun dettaglio interno nelle risposte
# =============================================================================


SEGRETO_FINTO = "sk-live-questo-non-deve-uscire-dal-processo"


def test_un_errore_imprevisto_non_espone_stack_trace_ne_dettagli(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un bug deve diventare un 500 sanitizzato, mai un dump interno.

    L'eccezione porta con sé una stringa che somiglia a una credenziale: se
    finisse nel corpo della risposta — direttamente o dentro un traceback — il
    test la ritroverebbe.
    """
    import app.api.v1.insights as insights_module

    def esplode(*args: Any, **kwargs: Any):
        raise RuntimeError(f"connessione fallita con chiave {SEGRETO_FINTO}")

    monkeypatch.setattr(insights_module, "scoped_client", esplode)

    with caplog.at_level(logging.ERROR):
        response = client.get("/api/v1/insights", headers=auth_headers)

    assert response.status_code == 500
    corpo = response.text
    assert SEGRETO_FINTO not in corpo
    assert "Traceback" not in corpo
    assert "RuntimeError" not in corpo
    assert "insights_module" not in corpo

    # Forma d'errore uniforme, senza campi extra.
    errore = response.json()["error"]
    assert errore["code"] == "internal_error"
    assert set(errore) <= {"code", "message", "details"}


def test_la_validazione_non_rimanda_indietro_il_valore_inviato(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """I dettagli di un 422 dicono *quale* campo e *perché*, non il contenuto.

    Il valore inviato può essere grande o contenere frammenti non pertinenti;
    riecheggiarlo è anche il modo più semplice per trasformare un errore di
    validazione in un riflettore XSS quando il client lo stampa.
    """
    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        # `analysis_mode` fuori dai valori ammessi: fa scattare la validazione
        # di FastAPI, che è il percorso con i `details` da ispezionare.
        json={
            "video_url": "<script>alert(1)</script>",
            "analysis_mode": "<script>alert(1)</script>",
        },
    )

    assert response.status_code == 422
    assert "<script>" not in response.text

    for dettaglio in response.json()["error"]["details"]:
        assert set(dettaglio) == {"field", "message", "type"}


def test_in_produzione_la_documentazione_interattiva_e_disattivata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()

    with TestClient(create_app(), raise_server_exceptions=False) as prod_client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert prod_client.get(path).status_code == 404, path


# =============================================================================
# CORS
# =============================================================================


def test_cors_ammette_l_origin_del_frontend(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": TEST_FRONTEND_URL})

    assert response.headers.get("access-control-allow-origin") == TEST_FRONTEND_URL
    assert response.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        # Prefisso dell'origin ammesso: passerebbe un confronto con `startswith`.
        "http://localhost:3000.evil.example",
        "http://localhost:3001",
        "null",
    ],
)
def test_cors_rifiuta_gli_altri_origin(client: TestClient, origin: str) -> None:
    response = client.get("/health", headers={"Origin": origin})

    # Senza l'header il browser blocca la lettura della risposta: è esattamente
    # ciò che deve accadere.
    assert "access-control-allow-origin" not in response.headers


def test_cors_rifiuta_la_preflight_da_un_origin_non_ammesso(client: TestClient) -> None:
    response = client.options(
        "/api/v1/insights",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_in_produzione_e_ammesso_solo_frontend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nessuna scorciatoia da sviluppo sopravvive in produzione.

    Fuori da `production` il backend ammette anche la variante `127.0.0.1` di
    `localhost`, comodità che non deve estendersi al dominio pubblico.
    """
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FRONTEND_URL", "https://picox.vercel.app")
    get_settings.cache_clear()

    with TestClient(create_app(), raise_server_exceptions=False) as prod_client:
        consentito = prod_client.get(
            "/health", headers={"Origin": "https://picox.vercel.app"}
        )
        assert (
            consentito.headers.get("access-control-allow-origin")
            == "https://picox.vercel.app"
        )

        for origin in ("http://localhost:3000", "http://127.0.0.1:3000", "https://picox.app"):
            respinto = prod_client.get("/health", headers={"Origin": origin})
            assert "access-control-allow-origin" not in respinto.headers, origin


# =============================================================================
# Logging correlato
# =============================================================================


class _CapturingHandler(logging.Handler):
    """Handler con lo stesso filtro usato in produzione."""

    def __init__(self) -> None:
        super().__init__()
        self.addFilter(RequestContextFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def access_logs(self) -> list[logging.LogRecord]:
        return [record for record in self.records if record.name == "app.access"]


def campo(record: logging.LogRecord, nome: str) -> Any:
    """Legge un attributo aggiunto al record dopo la sua creazione.

    `RequestContextFilter` inietta `request_id`/`user_id` e il middleware passa
    i campi HTTP via `extra=`: nessuno di questi è dichiarato su `LogRecord`,
    quindi l'accesso diretto è corretto a runtime ma non type-checkabile.
    """
    return getattr(record, nome)


def test_le_righe_di_log_di_una_richiesta_portano_request_id_e_user_id(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """La correlazione è il punto: request_id su ogni riga, user_id se autenticato.

    L'handler viene agganciato qui e non in una fixture perché `configure_logging`
    gira allo startup dell'app e azzera gli handler del root logger: montarlo
    prima significherebbe vederselo rimuovere.
    """
    handler = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = client.get("/api/v1/insights", headers=auth_headers)
    finally:
        root.removeHandler(handler)

    assert response.status_code == 200

    accessi = handler.access_logs()
    assert accessi, "Nessuna riga di access log prodotta."

    accesso = accessi[-1]
    assert campo(accesso, "request_id") != "-"
    assert len(campo(accesso, "request_id")) >= 8
    assert campo(accesso, "user_id") == USER_ID
    assert campo(accesso, "http_status") == 200
    assert campo(accesso, "http_path") == "/api/v1/insights"


def test_il_request_id_non_viene_restituito_al_client(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Requisito esplicito: l'id serve alla correlazione lato server e resta lì."""
    response = client.get("/api/v1/insights", headers=auth_headers)

    assert "x-request-id" not in {name.lower() for name in response.headers}
    assert "request_id" not in response.text


def test_una_richiesta_anonima_non_eredita_l_utente_della_precedente(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    handler = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        client.get("/api/v1/insights", headers=auth_headers)
        client.get("/api/v1/insights")  # senza JWT
    finally:
        root.removeHandler(handler)

    accessi = handler.access_logs()
    assert campo(accessi[-1], "user_id") == "-"
    assert campo(accessi[-1], "http_status") == 401


def test_due_richieste_hanno_request_id_diversi(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    handler = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        client.get("/api/v1/insights", headers=auth_headers)
        client.get("/api/v1/insights", headers=auth_headers)
    finally:
        root.removeHandler(handler)

    ids = [campo(record, "request_id") for record in handler.access_logs()]
    assert len(set(ids)) == len(ids) == 2


@pytest.mark.parametrize(
    ("valore", "atteso"),
    [
        ("abc12345", "abc12345"),
        ("a" * 64, "a" * 64),
        ("req-id_ABC-123", "req-id_ABC-123"),
        ("troppo-corto", "troppo-corto"),
        (None, None),
        ("", None),
        ("corto", None),
        ("a" * 65, None),
        # Log injection: una newline spezzerebbe una riga in due record falsi.
        ("abcdefgh\nfake log line", None),
        ("abcdefgh; rm -rf /", None),
        ("../../etc/passwd", None),
    ],
)
def test_il_request_id_in_ingresso_viene_validato(valore: str | None, atteso: str | None) -> None:
    assert sanitize_incoming_request_id(valore) == atteso


def test_il_formatter_json_produce_una_riga_parsabile() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="analisi completata per %s",
        args=("video-1",),
        exc_info=None,
    )
    record.request_id = "abc123def456"
    record.user_id = USER_ID
    record.duration_ms = 12.5

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "analisi completata per video-1"
    assert payload["request_id"] == "abc123def456"
    assert payload["user_id"] == USER_ID
    assert payload["level"] == "INFO"
    assert payload["duration_ms"] == 12.5


def test_il_formatter_json_include_lo_stack_trace() -> None:
    """Lo stack trace deve stare nei log — è l'unico posto in cui deve stare."""
    formatter = JsonLogFormatter()
    try:
        raise ValueError("guasto interno")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="fallito",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))

    assert "ValueError: guasto interno" in payload["exception"]


def test_il_contesto_non_sopravvive_alla_richiesta() -> None:
    """Le contextvars tornano al valore iniziale: nessuna perdita fra richieste."""
    assert request_id_var.get() is None
    assert user_id_var.get() is None
