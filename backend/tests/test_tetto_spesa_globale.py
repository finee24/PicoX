"""Tetto globale di spesa giornaliera (migration 0011, voce A4 dell'audit).

COSA E' VERIFICATO QUI, E COSA NO — leggere prima di aggiungere test.

Il tetto vive in un trigger (`enforce_global_spend_cap`) e questi test girano
sul doppio in memoria, che i trigger non li ha. Vale la stessa divisione gia'
adottata in `test_quota_analisi.py` e `test_limite_piano.py`, e per la stessa
ragione: **un test che finge il trigger verificherebbe la finzione, non il
database**. Insegnare al doppio a contare la spesa e rifiutare la riga N+1
darebbe una suite verde che non dice nulla sul comportamento reale, e
soprattutto resterebbe verde se la migration non fosse mai applicata.

Verificato qui, perche' e' codice nostro e il doppio lo riproduce fedelmente:

  * `PX004` diventa `GlobalCapacityError`, 409, `global_capacity_reached`;
  * il messaggio verso il client **non rivela il meccanismo** ne' i numeri;
  * i quattro SQLSTATE di dominio restano distinti fra loro;
  * il rifiuto non lascia dietro ne' insight ne' evento di consumo;
  * il WARNING strutturato riporta spesa stimata e tetto, e quei numeri
    **non** compaiono nella risposta HTTP;
  * un cache hit non scrive alcun evento, quindi non puo' consumare il tetto.

NON verificabile qui, e da verificare contro il progetto reale:

  * la riga N+1 che supererebbe il tetto viene rifiutata;
  * la riga esattamente al tetto passa (la condizione e' `>`, non `>=`);
  * analisi e validazioni contano **insieme**, non separatamente;
  * l'ordine dei trigger: al superamento simultaneo del tetto di piano e di
    quello globale, deve arrivare `PX002`/`PX003` e non `PX004`.

FALSIFICAZIONE. Questi test sono falsificati sulla meta' che coprono:
disattivando il ramo `_GLOBAL_SPEND_CAP` di `translate_postgrest_error`,
**8 dei 14 diventano rossi** — misurato, non stimato. `PX004` cadrebbe nel ramo
generico e tornerebbe un 503 `database_unavailable`, cioe' un guasto inventato
al posto di un tetto raggiunto. La falsificazione dell'altra meta' —
disattivare il trigger — richiede il database vero, dove e' l'unica che
significhi qualcosa.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import (
    AnalysisQuotaError,
    ConflictError,
    DatabaseError,
    GlobalCapacityError,
    PlanLimitError,
    ValidationQuotaError,
)
from app.services.supabase_service import _dettagli_spesa, translate_postgrest_error
from tests.conftest import FakeGemini
from tests.fake_supabase import FakeAPIError, FakeStore

VIDEO = "https://tiktok.com/@creator/video/123"

# La forma esatta in cui il trigger solleva: messaggio generico, numeri nel
# `detail`. Copiata dalla migration 0011 — se la` format()` la' cambia, questi
# test devono cambiare con lei.
MESSAGGIO_POSTGRES = "Tetto di spesa globale giornaliero raggiunto."
DETTAGLIO_POSTGRES = (
    "spend_today_usd=99.9820;row_cost_usd=0.035000;"
    "daily_cap_usd=100.0000;source_table=analysis_events"
)


def _px004() -> FakeAPIError:
    return FakeAPIError("PX004", MESSAGGIO_POSTGRES, DETTAGLIO_POSTGRES)


def _analizza(client: TestClient, headers: dict[str, str], url: str = VIDEO):
    return client.post(
        "/api/v1/analyze-video",
        headers=headers,
        json={"video_url": url, "analysis_mode": "INFO"},
    )


# =============================================================================
# 1. Lo SQLSTATE diventa l'eccezione giusta
# =============================================================================


def test_lo_sqlstate_px004_diventa_un_errore_di_capacita() -> None:
    errore = translate_postgrest_error(_px004(), context="tetto globale")

    assert isinstance(errore, GlobalCapacityError)
    assert errore.status_code == 409
    assert errore.code == "global_capacity_reached"


@pytest.mark.parametrize(
    ("codice", "atteso"),
    [
        ("PX001", PlanLimitError),
        ("PX002", AnalysisQuotaError),
        ("PX003", ValidationQuotaError),
        ("PX004", GlobalCapacityError),
        ("23505", ConflictError),
        # Un codice sconosciuto resta generico: e' la difesa contro il leak, e
        # non va allentata per far posto ai casi nuovi.
        ("42883", DatabaseError),
    ],
)
def test_i_quattro_sqlstate_di_dominio_restano_distinti(
    codice: str, atteso: type[Exception]
) -> None:
    """Quattro limiti, quattro codici, quattro risposte diverse.

    `PX004` e' l'unico dei quattro che verso il client **non** deve spiegarsi,
    ma deve comunque restare distinto qui dentro: e' il backend a doverlo
    trattare come incidente invece che come rifiuto ordinario.
    """
    errore = translate_postgrest_error(FakeAPIError(codice, "x"), context="t")
    assert isinstance(errore, atteso)


# =============================================================================
# 2. Il messaggio al client non rivela il meccanismo
# =============================================================================


def test_il_messaggio_al_client_non_rivela_il_tetto_ne_i_numeri() -> None:
    """Dire «tetto di spesa globale a $100» darebbe la mappa di dove colpire.

    E' la differenza deliberata rispetto alle tre quote per piano, che invece
    l'utente deve capire: la' il limite e' suo e ha un rimedio, qui e' del
    servizio e saperlo serve solo a chi lo sta sondando.
    """
    errore = translate_postgrest_error(_px004(), context="tetto globale")
    testo = str(errore).lower()

    for frammento in (
        "tetto", "spesa", "spend", "cap", "globale", "global",
        "px004", "sqlstate", "trigger", "enforce_global_spend_cap",
        "analysis_events", "validation_events", "spend_limits",
        "100", "99.98", "0.035", "usd", "$",
    ):
        assert frammento not in testo, f"il messaggio rivela «{frammento}»"

    # E dice comunque qualcosa di utile: riprovare, piu' tardi.
    assert "riprova" in testo


def test_il_messaggio_non_e_quello_di_un_guasto_del_database() -> None:
    """409 e non 503: la condizione dura fino a mezzanotte, i retry la peggiorano.

    Se `GlobalCapacityError` collassasse in `DatabaseError` si perderebbero due
    cose insieme: il codice distinto nel monitoraggio, e la semantica del retry.
    """
    errore = translate_postgrest_error(_px004(), context="tetto globale")

    assert not isinstance(errore, DatabaseError)
    assert isinstance(errore, GlobalCapacityError)
    assert errore.status_code == 409, "un 503 inviterebbe al ritentativo automatico"


# =============================================================================
# 3. Il WARNING strutturato — il dato che oggi manca (voce B4)
# =============================================================================


def test_il_warning_registra_spesa_stimata_e_tetto(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.supabase_service"):
        translate_postgrest_error(_px004(), context="insert analysis_events")

    record = next(
        (r for r in caplog.records if getattr(r, "evento", None) == "tetto_spesa_globale"),
        None,
    )
    assert record is not None, "il tetto globale e' scattato senza lasciare un WARNING"
    assert record.levelno == logging.WARNING, (
        "INFO come le quote per piano direbbe che e' il normale funzionamento"
    )

    # I campi arrivano da `extra=`, quindi `JsonLogFormatter` li promuove a
    # chiavi di primo livello: interrogabili senza rileggere il testo.
    assert getattr(record, "spend_today_usd", None) == "99.9820"
    assert getattr(record, "daily_cap_usd", None) == "100.0000"
    assert getattr(record, "row_cost_usd", None) == "0.035000"
    assert getattr(record, "source_table", None) == "analysis_events"


def test_un_dettaglio_malformato_non_fa_perdere_il_warning() -> None:
    """Il log d'allarme non deve dipendere dal formato del `detail`.

    Se un giorno la `format()` nella migration cambia, si perde la precisione
    dei campi — non l'allarme. Perdere l'allarme sarebbe molto peggio: e' il
    solo posto in cui il superamento del tetto lascia traccia.
    """
    assert _dettagli_spesa(None) == {}
    assert _dettagli_spesa("") == {}
    assert _dettagli_spesa("senza separatori") == {}
    assert _dettagli_spesa("chiave_inventata=1;daily_cap_usd=50") == {
        "daily_cap_usd": "50"
    }

    errore = translate_postgrest_error(
        FakeAPIError("PX004", MESSAGGIO_POSTGRES, "formato cambiato"),
        context="t",
    )
    assert isinstance(errore, GlobalCapacityError)


# =============================================================================
# 4. Il percorso completo, e cosa resta dietro
# =============================================================================


def test_il_rifiuto_arriva_al_client_come_409_pulito(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rifiuto del database -> PostgREST -> envelope HTTP, tutto codice vero.

    Il rifiuto va iniettato **dentro l'insert**, dove il trigger lo
    solleverebbe, e non al posto della funzione che consuma la quota:
    sostituire la funzione intera scavalcherebbe il suo `db_errors`, cioe'
    proprio la traduzione che questo test deve verificare. E' la trappola gia'
    documentata in `test_quota_analisi.py`.
    """
    originale = store.enforce_unique

    def rifiuta_analysis_events(table: str, values: Any) -> None:
        if table == "analysis_events":
            raise FakeAPIError("PX004", MESSAGGIO_POSTGRES, DETTAGLIO_POSTGRES)
        originale(table, values)

    monkeypatch.setattr(store, "enforce_unique", rifiuta_analysis_events)

    risposta = _analizza(client, auth_headers)

    assert risposta.status_code == 409, risposta.text
    assert risposta.json()["error"]["code"] == "global_capacity_reached"

    # Nulla del meccanismo deve attraversare il confine HTTP.
    for frammento in ("PX004", "spend_today_usd", "daily_cap_usd", "100.0000", "0.035"):
        assert frammento not in risposta.text, f"la risposta rivela «{frammento}»"


def test_il_rifiuto_non_consuma_nulla(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una richiesta fermata dal tetto non deve costare, ne' lasciare tracce.

    Il trigger e' BEFORE INSERT: la riga non entra, quindi non alza la spesa
    stimata del giorno. Se la lasciasse entrare, il tetto si auto-alimenterebbe
    — ogni rifiuto avvicinerebbe il rifiuto successivo.
    """
    originale = store.enforce_unique

    def rifiuta_analysis_events(table: str, values: Any) -> None:
        if table == "analysis_events":
            raise FakeAPIError("PX004", MESSAGGIO_POSTGRES, DETTAGLIO_POSTGRES)
        originale(table, values)

    monkeypatch.setattr(store, "enforce_unique", rifiuta_analysis_events)

    assert _analizza(client, auth_headers).status_code == 409

    assert store.rows("analysis_events") == [], (
        "l'evento di consumo e' stato scritto nonostante il rifiuto: "
        "il tetto si auto-alimenterebbe"
    )
    assert store.rows("insights") == [], "analisi eseguita nonostante il tetto raggiunto"
    assert gemini.calls == [], "Gemini pagato nonostante il tetto raggiunto"


def test_un_cache_hit_non_scrive_un_evento_quindi_non_consuma_il_tetto(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
) -> None:
    """Stessa regola delle quote per piano, e per la stessa ragione.

    Il tetto globale conta le righe di `analysis_events` e `validation_events`.
    Un cache hit non ne scrive nessuna — non ha speso nulla — quindi non puo'
    avvicinare il blocco. Il tetto sorveglia la spesa, non il traffico: se
    contasse le richieste, il servizio si fermerebbe per lavoro che non e'
    costato niente.
    """
    assert _analizza(client, auth_headers).status_code == 201
    seconda = _analizza(client, auth_headers)

    assert seconda.status_code == 200, "la seconda doveva essere un cache hit"
    assert len(gemini.calls) == 1, "Gemini richiamato: non era un cache hit"
    assert len(store.rows("analysis_events")) == 1, (
        "il cache hit ha scritto un evento, quindi consumerebbe il tetto globale"
    )
