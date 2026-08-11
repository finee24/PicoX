"""Quota giornaliera di analisi sul percorso manuale.

Il tetto vive in un trigger (`enforce_analysis_quota`, migration 0008) e questi
test girano su un doppio in memoria, che i trigger non li ha. Qui si verifica
quindi cio' che il doppio **puo'** riprodurre fedelmente — chi scrive una riga
di consumo e chi no, e come l'errore del database diventa una risposta HTTP —
mentre l'imposizione vera del tetto e' verificata contro il progetto reale.

E' la stessa divisione gia' usata in `test_limite_piano.py` per il tetto ai
creator, e per la stessa ragione: un test che finge il trigger verificherebbe
la finzione, non il database.

LA PROPRIETA' CHE CONTA DI PIU'. La quota si consuma nel punto in cui la spesa
e' **decisa**, non in quello in cui riesce: un'analisi che paga Apify e poi
fallisce su Gemini deve contare, perche' e' costata. Contare le righe di
`insights` — la strada piu' semplice — avrebbe perso esattamente quel caso, ed
e' il motivo per cui esiste `analysis_events`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import (
    AnalysisQuotaError,
    ConflictError,
    DatabaseError,
    GeminiError,
    PlanLimitError,
)
from app.services.supabase_service import translate_postgrest_error
from tests.conftest import USER_ID, FakeApify, FakeGemini
from tests.fake_supabase import FakeAPIError, FakeStore

VIDEO = "https://tiktok.com/@creator/video/123"
MESSAGGIO_POSTGRES = "Quota di 30 analisi giornaliere raggiunta per il piano free."


def _analizza(client: TestClient, headers: dict[str, str], url: str = VIDEO):
    return client.post(
        "/api/v1/analyze-video",
        headers=headers,
        json={"video_url": url, "analysis_mode": "INFO"},
    )


# =============================================================================
# 1. Chi consuma quota e chi no
# =============================================================================


def test_il_percorso_manuale_registra_un_evento_di_consumo(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    risposta = _analizza(client, auth_headers)

    assert risposta.status_code == 201, risposta.text
    eventi = store.rows("analysis_events")
    assert len(eventi) == 1
    assert eventi[0]["user_id"] == USER_ID
    assert eventi[0]["analysis_mode"] == "INFO"


def test_il_cache_hit_non_consuma_quota(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore,
    gemini: FakeGemini,
) -> None:
    """Un risultato gia' in cache non costa nulla, quindi non deve costare quota.

    E' la ragione per cui il consumo sta **dopo** i due lookup di cache e non
    all'ingresso dell'endpoint.
    """
    assert _analizza(client, auth_headers).status_code == 201
    seconda = _analizza(client, auth_headers)

    assert seconda.status_code == 200, "la seconda doveva essere un cache hit"
    assert len(gemini.calls) == 1, "Gemini richiamato: non era un cache hit"
    assert len(store.rows("analysis_events")) == 1, (
        "il cache hit ha consumato quota"
    )


async def test_un_analisi_fallita_consuma_comunque_la_quota(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore,
    gemini: FakeGemini,
) -> None:
    """Se Apify e' stato pagato e Gemini fallisce, la spesa c'e' stata lo stesso.

    E' il caso che contare `insights` avrebbe perso: nessuna riga di insight
    viene scritta, eppure il denaro e' uscito. Chi volesse abusare del sistema
    generando errori troverebbe un contatore che non lo vede.
    """
    gemini.error = GeminiError()

    risposta = _analizza(client, auth_headers)

    assert risposta.status_code == 503, risposta.text
    assert store.rows("insights") == [], "un'analisi fallita non deve lasciare insight"
    assert len(store.rows("analysis_events")) == 1, (
        "l'analisi fallita non ha consumato quota: e' la falla che "
        "`analysis_events` esiste per chiudere"
    )


def test_il_cron_non_consuma_la_quota_manuale(
    client: TestClient, cron_headers: dict[str, str], store: FakeStore,
    apify: FakeApify,
) -> None:
    """Due budget indipendenti: il cron ha gia' il tetto ai creator attivi.

    Se consumasse la quota manuale, un giro notturno azzererebbe le analisi che
    l'utente puo' fare di giorno, senza che nulla glielo spieghi.
    """
    from app.services.apify_service import ScrapedVideo

    store.seed("creators", {
        "user_id": USER_ID, "username": "creator_cron", "platform": "tiktok",
        "analysis_mode": "INFO", "is_active": True,
    })
    apify.videos = [ScrapedVideo(
        video_url=VIDEO, download_url="https://cdn.example.com/v.mp4",
        thumbnail_url=None, duration_seconds=30.0,
    )]

    risposta = client.post("/api/v1/cron/check-updates", headers=cron_headers)

    assert risposta.status_code == 200, risposta.text
    assert risposta.json()["queued_analyses"] == 1
    assert len(store.rows("insights")) == 1, "il cron non ha analizzato"
    assert store.rows("analysis_events") == [], (
        "il cron ha consumato la quota manuale dell'utente"
    )


# =============================================================================
# 2. L'errore del database diventa una risposta comprensibile
# =============================================================================


def test_lo_sqlstate_px002_diventa_un_errore_di_quota() -> None:
    errore = translate_postgrest_error(
        FakeAPIError("PX002", MESSAGGIO_POSTGRES), context="quota"
    )

    assert isinstance(errore, AnalysisQuotaError)
    assert errore.status_code == 409
    assert errore.code == "analysis_quota_reached"


def test_il_messaggio_al_client_non_contiene_dettagli_del_database() -> None:
    errore = translate_postgrest_error(
        FakeAPIError("PX002", MESSAGGIO_POSTGRES), context="quota"
    )

    testo = str(errore).lower()
    for frammento in ("px002", "sqlstate", "plpgsql", "enforce_analysis_quota",
                      "analysis_events", "trigger"):
        assert frammento not in testo
    # E dice comunque cosa e' successo e quando passera'.
    assert "analisi" in testo and "domani" in testo


@pytest.mark.parametrize(
    ("codice", "atteso"),
    [
        ("PX001", PlanLimitError),
        ("PX002", AnalysisQuotaError),
        ("23505", ConflictError),
        # Un codice sconosciuto resta generico: e' la difesa contro il leak, e
        # non va allentata per far posto ai casi nuovi.
        ("42883", DatabaseError),
    ],
)
def test_i_due_limiti_di_piano_restano_distinti(
    codice: str, atteso: type[Exception]
) -> None:
    """PX001 e PX002 non devono collassare in un unico messaggio.

    Sono entrambi 409 e riguardano entrambi il piano, ma «hai troppi creator» e
    «hai esaurito le analisi di oggi» richiedono all'utente due azioni diverse.
    """
    errore = translate_postgrest_error(FakeAPIError(codice, "x"), context="t")
    assert isinstance(errore, atteso)


def test_la_quota_esaurita_arriva_al_client_come_409_pulito(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Il percorso completo: rifiuto del database -> PostgREST -> envelope HTTP.

    Il doppio non ha trigger, quindi il rifiuto va iniettato — ma **dentro
    l'insert**, esattamente dove il database lo solleverebbe, non al posto di
    `_consuma_quota`. Sostituire la funzione intera scavalcherebbe il suo
    `db_errors`, cioe' proprio la traduzione che questo test deve verificare, e
    il risultato sarebbe un 500: e' l'errore che ho fatto scrivendolo la prima
    volta. Da qui in giu' il codice attraversato e' tutto quello vero.
    """
    originale = store.enforce_unique

    def rifiuta_analysis_events(table: str, values: Any) -> None:
        if table == "analysis_events":
            raise FakeAPIError("PX002", MESSAGGIO_POSTGRES)
        originale(table, values)

    monkeypatch.setattr(store, "enforce_unique", rifiuta_analysis_events)

    risposta = _analizza(client, auth_headers)

    assert risposta.status_code == 409, risposta.text
    corpo = risposta.json()
    assert corpo["error"]["code"] == "analysis_quota_reached"
    assert "PX002" not in risposta.text
    assert store.rows("insights") == [], "analisi eseguita nonostante la quota esaurita"
