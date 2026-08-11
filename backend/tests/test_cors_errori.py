"""Gli header CORS su un 500 generato fuori da `CORSMiddleware` (voce A6).

Lo stack e' `ServerErrorMiddleware -> RequestContextMiddleware -> CORSMiddleware
-> ExceptionMiddleware -> router`. Un'eccezione dentro un router con
`route_class=SafeRoute` viene gestita da `ExceptionMiddleware`, che sta **dentro**
al CORS; una in un endpoint registrato direttamente sull'app arriva fino a
`ServerErrorMiddleware`, che sta **fuori**. La risposta era identica e sanificata
in entrambi i casi, ma nel secondo usciva senza header CORS: il browser non
riusciva a leggerla e il frontend mostrava "errore di rete" invece
dell'envelope. Il caso non e' teorico — `/health` e' registrato cosi'.

`ServerErrorMiddleware` non si e' spostato: e' la sua posizione esterna a
garantire che nessun endpoint futuro possa scavalcarlo. Sono gli header a
scendere li'.

IL TEST CHE CONTA DI PIU' e' quello sull'origin non ammesso: rimandare indietro
l'`Origin` ricevuto senza verificarlo, insieme a `Allow-Credentials: true`,
trasformerebbe la risposta d'errore esattamente nel buco che il CORS ristretto
chiude.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.middleware.error_handler import SafeRoute
from tests.conftest import TEST_FRONTEND_URL

ORIGIN_AMMESSO = TEST_FRONTEND_URL
ORIGIN_OSTILE = "https://sito-malevolo.example"
ESCA = "postgresql://interno:Pa55w0rd@db.interno:5432/picox"


def _app_con_endpoint_che_esplodono(app: Any) -> Any:
    """Aggiunge due endpoint identici tranne che per `SafeRoute`."""

    @app.get("/_prova/senza-saferoute")
    async def senza() -> dict[str, str]:
        raise RuntimeError(ESCA)

    router = APIRouter(prefix="/_prova", route_class=SafeRoute)

    @router.get("/con-saferoute")
    async def con() -> dict[str, str]:
        raise RuntimeError(ESCA)

    app.include_router(router)
    return app


def test_il_500_fuori_da_saferoute_porta_gli_header_cors(app: Any) -> None:
    with TestClient(_app_con_endpoint_che_esplodono(app), raise_server_exceptions=False) as c:
        r = c.get("/_prova/senza-saferoute", headers={"Origin": ORIGIN_AMMESSO})

    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == ORIGIN_AMMESSO
    assert r.headers.get("access-control-allow-credentials") == "true"
    assert "Origin" in r.headers.get("vary", "")


def test_un_origin_non_ammesso_non_viene_mai_rimandato_indietro(app: Any) -> None:
    """Il caso che rende il fix sicuro invece che comodo.

    Se qui comparisse `Access-Control-Allow-Origin: https://sito-malevolo.example`
    la risposta d'errore diventerebbe leggibile da qualunque sito, con le
    credenziali dell'utente al seguito.
    """
    with TestClient(_app_con_endpoint_che_esplodono(app), raise_server_exceptions=False) as c:
        r = c.get("/_prova/senza-saferoute", headers={"Origin": ORIGIN_OSTILE})

    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers
    assert ORIGIN_OSTILE not in str(r.headers)


def test_senza_header_origin_non_si_inventano_header_cors(app: Any) -> None:
    """Una chiamata non-browser (curl, lo scheduler) non manda `Origin`."""
    with TestClient(_app_con_endpoint_che_esplodono(app), raise_server_exceptions=False) as c:
        r = c.get("/_prova/senza-saferoute")

    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


def test_il_percorso_con_saferoute_resta_invariato(app: Any) -> None:
    """Gruppo di controllo: li' gli header li mette `CORSMiddleware`, come prima.

    Senza questo confronto, i test sopra sarebbero verdi anche se il fix avesse
    per sbaglio dirottato tutti i 500 fuori dal CORS.
    """
    with TestClient(_app_con_endpoint_che_esplodono(app), raise_server_exceptions=False) as c:
        r = c.get("/_prova/con-saferoute", headers={"Origin": ORIGIN_AMMESSO})

    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == ORIGIN_AMMESSO


def test_il_corpo_resta_sanificato_in_entrambi_i_percorsi(app: Any) -> None:
    """Regressione della sezione 5: gli header nuovi non aprono il corpo.

    Il messaggio dell'eccezione contiene un DSN con password; nessuna delle due
    risposte deve riportarne traccia.
    """
    with TestClient(_app_con_endpoint_che_esplodono(app), raise_server_exceptions=False) as c:
        risposte = [
            c.get("/_prova/senza-saferoute", headers={"Origin": ORIGIN_AMMESSO}),
            c.get("/_prova/con-saferoute", headers={"Origin": ORIGIN_AMMESSO}),
        ]

    for r in risposte:
        assert r.json()["error"]["code"] == "internal_error"
        for frammento in ("Pa55w0rd", "postgresql://", "Traceback", "RuntimeError"):
            assert frammento not in r.text
