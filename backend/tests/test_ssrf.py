"""La difesa SSRF di `_assert_public_target` (voce A10 dell'audit).

`video_url` arriva dall'utente, quindi senza questo controllo `analyze-video` e'
un SSRF: si fa scaricare al backend `http://169.254.169.254/…` — i metadata
dell'istanza, cioe' spesso credenziali cloud — o un servizio su `127.0.0.1`,
usando la differenza fra gli errori come oracolo per mappare la rete interna. Nel
caso peggiore il contenuto interno finisce su Gemini.

PERCHE QUESTI TEST NON ESISTEVANO. La fixture `downloads` di `conftest.py` e'
`autouse` e sostituisce `download_to_temp` in tutta la suite: il modulo vero non
veniva mai esercitato, e la difesa piu' delicata del backend aveva copertura
zero. Qui si chiama direttamente la funzione, senza passare dalla fixture.

Nessun test tocca la rete: `socket.getaddrinfo` e' sostituito, e i redirect
passano da un `httpx.MockTransport`.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError, PicoxValidationError
from app.services.media_service import _assert_public_target, _open_validated_stream

PUBBLICO = "93.184.216.34"


def _risolvi_come(monkeypatch: pytest.MonkeyPatch, mappa: dict[str, list[str]]) -> None:
    """Sostituisce la risoluzione DNS con una tabella fissa.

    Il controllo vero e' sull'**IP risolto** e non sul nome — un host pubblico
    puo' avere un record A che punta a `127.0.0.1` — quindi il doppio deve
    intervenire proprio qui, altrimenti si verificherebbe una cosa diversa da
    quella che protegge.
    """

    def falso(host: str, *_: Any, **__: Any) -> list[tuple[Any, ...]]:
        if host not in mappa:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in mappa[host]]

    monkeypatch.setattr(socket, "getaddrinfo", falso)


# =============================================================================
# 1. Gli indirizzi che devono essere rifiutati
# =============================================================================


@pytest.mark.parametrize(
    ("nome", "ip"),
    [
        ("loopback", "127.0.0.1"),
        ("loopback non ovvio", "127.255.255.254"),
        ("metadata cloud", "169.254.169.254"),
        ("link-local", "169.254.0.1"),
        ("privato 10/8", "10.0.0.1"),
        ("privato 172.16/12", "172.16.0.1"),
        ("privato 192.168/16", "192.168.1.1"),
        ("CGNAT 100.64/10", "100.64.0.1"),
        ("unspecified", "0.0.0.0"),
        ("multicast", "224.0.0.1"),
        ("loopback IPv6", "::1"),
        ("unique local IPv6", "fc00::1"),
        ("link-local IPv6", "fe80::1"),
    ],
)
async def test_un_host_che_risolve_sulla_rete_interna_viene_rifiutato(
    nome: str, ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _risolvi_come(monkeypatch, {"interno.example": [ip]})

    with pytest.raises(PicoxValidationError) as errore:
        await _assert_public_target("https://interno.example/video.mp4")

    # Il messaggio non rivela l'indirizzo risolto: sarebbe un oracolo per
    # mappare la rete interna un host alla volta.
    testo = str(errore.value)
    assert ip not in testo
    assert "interno.example" not in testo


async def test_un_host_pubblico_passa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Il gruppo di controllo: senza, i test qui sopra sarebbero verdi anche con
    una funzione che rifiuta tutto."""
    _risolvi_come(monkeypatch, {"cdn.example": [PUBBLICO]})

    await _assert_public_target("https://cdn.example/video.mp4")


async def test_basta_un_indirizzo_interno_fra_tanti(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS che restituisce piu' record, uno solo dei quali interno.

    E' l'attacco che passerebbe se il controllo guardasse solo il primo
    indirizzo: httpx potrebbe poi connettersi a uno qualsiasi.
    """
    _risolvi_come(monkeypatch, {"misto.example": [PUBBLICO, "127.0.0.1"]})

    with pytest.raises(PicoxValidationError):
        await _assert_public_target("https://misto.example/video.mp4")


# =============================================================================
# 2. Schema e host
# =============================================================================


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://interno.example/",
        "ftp://interno.example/video.mp4",
        "data:video/mp4;base64,AAAA",
    ],
)
async def test_solo_http_e_https(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lo schema si controlla **prima** di risolvere: `file://` non ha un host da
    risolvere, e senza questo controllo il rifiuto arriverebbe per il motivo
    sbagliato o non arriverebbe affatto."""
    _risolvi_come(monkeypatch, {})

    with pytest.raises(PicoxValidationError):
        await _assert_public_target(url)


async def test_un_url_senza_host_viene_rifiutato(monkeypatch: pytest.MonkeyPatch) -> None:
    _risolvi_come(monkeypatch, {})

    with pytest.raises(PicoxValidationError):
        await _assert_public_target("https:///video.mp4")


async def test_un_host_irrisolvibile_non_e_un_errore_di_validazione(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinzione voluta: un DNS che non risponde e' un guasto esterno (503),
    non un input non valido (422). Confonderli manderebbe l'utente a correggere
    un URL che va benissimo."""
    _risolvi_come(monkeypatch, {})

    with pytest.raises(ExternalServiceError):
        await _assert_public_target("https://inesistente.example/video.mp4")


# =============================================================================
# 3. Il controllo vale su OGNI hop di redirect
# =============================================================================


async def test_un_redirect_verso_la_rete_interna_viene_fermato(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'attacco piu' comune: host innocuo che risponde 302 verso i metadata.

    `follow_redirects=True` di httpx salterebbe il controllo su ogni hop dopo il
    primo. La prova che conta non e' solo l'eccezione, ma che **la richiesta
    all'indirizzo interno non venga mai emessa**.
    """
    _risolvi_come(
        monkeypatch,
        {"innocuo.example": [PUBBLICO], "169.254.169.254": ["169.254.169.254"]},
    )
    raggiunti: list[str] = []

    def gestore(request: httpx.Request) -> httpx.Response:
        raggiunti.append(str(request.url))
        if request.url.host == "innocuo.example":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200, content=b"segreto interno")

    transport = httpx.MockTransport(gestore)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PicoxValidationError):
            await _open_validated_stream(
                client, "https://innocuo.example/video.mp4", get_settings()
            )

    assert raggiunti == ["https://innocuo.example/video.mp4"], (
        f"il backend ha contattato l'indirizzo interno: {raggiunti}"
    )


async def test_una_catena_di_redirect_troppo_lunga_si_ferma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Senza un tetto, un redirect che punta a se stesso terrebbe occupato il
    worker per sempre."""
    _risolvi_come(monkeypatch, {"anello.example": [PUBBLICO]})
    richieste: list[str] = []

    def gestore(request: httpx.Request) -> httpx.Response:
        richieste.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://anello.example/next"})

    transport = httpx.MockTransport(gestore)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ExternalServiceError):
            await _open_validated_stream(
                client, "https://anello.example/video.mp4", get_settings()
            )

    assert len(richieste) <= 6, f"nessun tetto ai redirect: {len(richieste)} richieste"
