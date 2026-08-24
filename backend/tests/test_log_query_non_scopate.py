"""Il log delle query service-role non scopate deve restare un segnale raro.

Terza delle cinque voci aperte dalla review di sicurezza della PR #7.

`unscoped_service_table` logga ogni accesso che bypassa il filtro per utente.
Nasce come **evento da notare**, e il suo valore stava tutto nel comparire di
rado. Da quando `creator_validations` e' in allowlist, la lettura della cache lo
fa scattare a ogni richiesta di validazione — cache hit compresi, che non costano
nulla e non hanno niente di eccezionale.

LA PROPRIETA' CHE CONTA. Un segnale che scatta sempre non e' piu' un segnale:
chi legge i log smette di guardarlo, ed e' il modo in cui una difesa muore senza
che nessuno l'abbia disattivata. Questi test verificano che i percorsi frequenti
scendano a DEBUG e che quelli eccezionali restino a INFO — non che il log
sparisca, che sarebbe il difetto opposto.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.services.apify_service import ScrapedProfile
from app.services.supabase_service import unscoped_service_table
from tests.conftest import FakeApify

LOGGER = "app.services.supabase_service"


def _righe_non_scopate(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """I record emessi da `unscoped_service_table`, riconosciuti dal campo
    strutturato e non dal testo: il messaggio puo' cambiare, il campo no."""
    return [r for r in caplog.records if getattr(r, "unscoped", False)]


# =============================================================================
# 1. Il livello dipende dal percorso, non dalla tabella
# =============================================================================


async def test_un_percorso_di_routine_non_sporca_i_log_a_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await unscoped_service_table(
            "creator_validations", reason="prova di routine", di_routine=True
        )

    righe = _righe_non_scopate(caplog)
    assert len(righe) == 1, "il log non deve sparire, solo abbassarsi"
    assert righe[0].levelno == logging.DEBUG


async def test_un_percorso_eccezionale_resta_a_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Il cron enumera i creator di tutti i tenant: e' raro ed e' esattamente il
    caso per cui questo log esiste."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await unscoped_service_table("creators", reason="prova eccezionale")

    righe = _righe_non_scopate(caplog)
    assert len(righe) == 1
    assert righe[0].levelno == logging.INFO


async def test_i_campi_strutturati_restano_filtrabili_a_entrambi_i_livelli(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Abbassare il livello non deve togliere la possibilita' di interrogarli.

    `JsonLogFormatter` promuove a chiave di primo livello tutto cio' che passa
    da `extra=`, quindi una ricerca su *tutte* le query non scopate resta
    possibile senza rileggere il testo del messaggio.
    """
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await unscoped_service_table("job_locks", reason="raro", di_routine=False)
        await unscoped_service_table("creator_validations", reason="frequente", di_routine=True)

    righe = _righe_non_scopate(caplog)
    assert len(righe) == 2
    for record in righe:
        # `type: ignore`: sono attributi che `extra=` aggiunge a runtime, e
        # `LogRecord` non li dichiara. L'accesso diretto e' il punto del test —
        # se sparissero, questo modulo deve rompersi.
        assert record.unscoped is True  # type: ignore[attr-defined]
        assert record.tabella in {"job_locks", "creator_validations"}  # type: ignore[attr-defined]
        assert record.motivo  # type: ignore[attr-defined]
    assert {getattr(r, "di_routine", None) for r in righe} == {True, False}


# =============================================================================
# 2. Il percorso reale: la validazione non deve piu' fare rumore
# =============================================================================


def test_la_validazione_non_emette_piu_un_info_per_ogni_richiesta(
    client: TestClient,
    auth_headers: dict[str, str],
    apify: FakeApify,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """E' il caso che ha fatto perdere valore al segnale.

    Falsificabile: togliendo `di_routine=True` dalla lettura della cache in
    `creator_validation._leggi_cache`, questo test torna rosso.
    """
    apify.profile = ScrapedProfile(username="creator", follower_count=10)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.post(
            "/api/v1/creators/validate",
            headers=auth_headers,
            json={"input": "instagram.com/creator"},
        )
        # La seconda e' un cache hit: prima era proprio questa a moltiplicare il
        # rumore, perche' non costa nulla e puo' ripetersi all'infinito.
        client.post(
            "/api/v1/creators/validate",
            headers=auth_headers,
            json={"input": "instagram.com/creator"},
        )

    # Si contano i record a INFO, NON quelli marcati `di_routine`: filtrare sul
    # marcatore renderebbe il test vacuo — togliendo `di_routine=True` dal
    # codice la lista si svuoterebbe e il test resterebbe verde proprio nel caso
    # che deve scoprire. È l'errore che questo commento esiste per non ripetere.
    a_info = [
        r for r in _righe_non_scopate(caplog)
        if getattr(r, "tabella", None) == "creator_validations"
        and r.levelno >= logging.INFO
    ]
    assert len(a_info) == 1, (
        f"due richieste hanno prodotto {len(a_info)} righe a INFO su "
        "creator_validations: attesa solo la scrittura, la lettura sta a DEBUG"
    )


def test_la_scrittura_della_cache_resta_visibile_a_info(
    client: TestClient,
    auth_headers: dict[str, str],
    apify: FakeApify,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """La scrittura avviene solo quando un provider e' stato davvero pagato.

    E' rara e proporzionale alla spesa: abbassarla insieme alla lettura avrebbe
    tolto l'unico segnale che resta su quel percorso.
    """
    apify.profile = ScrapedProfile(username="creator", follower_count=10)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.post(
            "/api/v1/creators/validate",
            headers=auth_headers,
            json={"input": "instagram.com/creator"},
        )

    scritture = [
        r for r in _righe_non_scopate(caplog)
        if getattr(r, "tabella", None) == "creator_validations"
        and r.levelno == logging.INFO
    ]
    assert len(scritture) == 1, "la scrittura della cache non e' piu' visibile a INFO"
