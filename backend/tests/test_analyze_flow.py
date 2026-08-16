"""Flusso di analisi: scrittura, cache e filtro `mode=SCRIPT`.

I tre comportamenti che questo file protegge sono quelli su cui il prodotto
costa denaro o smette di funzionare:

1. la prima analisi finisce davvero su `insights`;
2. la seconda analisi dello stesso video **non** richiama Gemini né Apify —
   ogni regressione qui si paga in token a ogni ricaricamento di pagina;
3. `mode=SCRIPT` filtra sulla *presenza* dello script, non sulla modalità.

Gemini, Apify e il download del media sono sostituiti da doppi che contano le
invocazioni: "non ha chiamato il servizio esterno" è così una proprietà
verificata e non un'affermazione.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import ApifyError, GeminiError, VideoTooLargeError
from tests.conftest import USER_ID, FakeApify, FakeGemini
from tests.fake_supabase import FakeStore

VIDEO_URL = "https://www.tiktok.com/@creator/video/123"


# =============================================================================
# 1. Prima chiamata: analisi eseguita e persistita
# =============================================================================


def test_prima_analisi_scrive_su_supabase(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    downloads: list[str],
) -> None:
    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": VIDEO_URL, "analysis_mode": "BOTH"},
    )

    # 201 e non 200: l'analisi è stata effettivamente prodotta.
    assert response.status_code == 201, response.text
    body = response.json()

    assert store.count("insights") == 1
    stored = store.rows("insights")[0]

    # Lo `user_id` viene dal JWT verificato, non dal body — che infatti non lo
    # contiene nemmeno (`AnalyzeVideoRequest` ha `extra="forbid"`).
    assert stored["user_id"] == USER_ID
    assert stored["analysis_mode"] == "BOTH"
    assert stored["summary_data"] is not None
    assert stored["style_data"] is not None
    assert stored["inverse_script_template"] is not None
    assert stored["keywords"]

    # La risposta rispecchia la riga salvata.
    assert body["id"] == stored["id"]
    assert body["video_url"] == stored["video_url"]
    assert body["summary_data"]["main_topic"] == "routine di studio serale"

    assert len(gemini.calls) == 1
    assert len(downloads) == 1


def test_modalita_info_non_produce_stile_ne_script(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    downloads: list[str],
) -> None:
    """La modalità richiesta decide cosa viene scritto, non cosa il modello manda."""
    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": VIDEO_URL, "analysis_mode": "INFO"},
    )

    assert response.status_code == 201, response.text
    stored = store.rows("insights")[0]
    assert stored["summary_data"] is not None
    assert stored["style_data"] is None
    assert stored["inverse_script_template"] is None


# =============================================================================
# 2. Seconda chiamata: cache, senza servizi esterni
# =============================================================================


def test_seconda_chiamata_stesso_video_usa_la_cache(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
    downloads: list[str],
) -> None:
    payload = {"video_url": VIDEO_URL, "analysis_mode": "BOTH"}

    first = client.post("/api/v1/analyze-video", headers=auth_headers, json=payload)
    assert first.status_code == 201, first.text

    calls_after_first = len(gemini.calls)
    resolves_after_first = len(apify.resolve_calls)
    downloads_after_first = len(downloads)

    second = client.post("/api/v1/analyze-video", headers=auth_headers, json=payload)

    # 200 e non 201: nessuna analisi nuova è stata prodotta. È la differenza su
    # cui il frontend decide se dire "analizzato" o "già in archivio".
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    # Nessun record duplicato e — il punto — nessuna chiamata esterna in più.
    assert store.count("insights") == 1
    assert len(gemini.calls) == calls_after_first
    assert len(apify.resolve_calls) == resolves_after_first
    assert len(downloads) == downloads_after_first


def test_la_cache_ignora_i_parametri_di_tracciamento(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
) -> None:
    """Lo stesso video condiviso due volte non si paga due volte.

    Un link copiato da app porta `?is_from_webapp=...&web_id=...`: valori
    diversi a ogni condivisione. Senza normalizzazione ognuno sarebbe una chiave
    di cache nuova e il vincolo `UNIQUE (user_id, video_url)` non scatterebbe
    mai.
    """
    first = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": f"{VIDEO_URL}?is_from_webapp=1&web_id=999", "analysis_mode": "BOTH"},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={
            "video_url": f"{VIDEO_URL}?is_from_webapp=1&web_id=different",
            "analysis_mode": "BOTH",
        },
    )

    assert second.status_code == 200, second.text
    assert store.count("insights") == 1
    assert len(gemini.calls) == 1


def test_la_cache_non_attraversa_gli_utenti(
    client: TestClient,
    auth_headers: dict[str, str],
    other_auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
) -> None:
    """La cache è per `(user_id, video_url)`: l'insight di un altro non è il tuo."""
    payload = {"video_url": VIDEO_URL, "analysis_mode": "BOTH"}

    first = client.post("/api/v1/analyze-video", headers=auth_headers, json=payload)
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/analyze-video", headers=other_auth_headers, json=payload)

    assert second.status_code == 201  # analisi vera, non cache
    assert store.count("insights") == 2
    assert len(gemini.calls) == 2


# =============================================================================
# 3. Filtro mode=SCRIPT
# =============================================================================


def _seed_insight(
    store: FakeStore, *, user_id: str, video_url: str, mode: str, has_script: bool
) -> dict:
    return store.seed(
        "insights",
        {
            "user_id": user_id,
            "creator_id": None,
            "video_url": video_url,
            "thumbnail_url": None,
            "analysis_mode": mode,
            "summary_data": {"main_topic": "argomento", "summary": "sintesi"},
            "style_data": None,
            "inverse_script_template": {"title": "Schema riusabile"} if has_script else None,
            "keywords": ["studio"],
        },
    )


def test_mode_script_seleziona_solo_i_record_con_script(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
) -> None:
    con_script = _seed_insight(
        store, user_id=USER_ID, video_url="https://tiktok.com/v/1", mode="BOTH", has_script=True
    )
    _seed_insight(
        store, user_id=USER_ID, video_url="https://tiktok.com/v/2", mode="BOTH", has_script=False
    )
    _seed_insight(
        store, user_id=USER_ID, video_url="https://tiktok.com/v/3", mode="INFO", has_script=False
    )

    response = client.get("/api/v1/insights?mode=SCRIPT", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()

    # Un record BOTH *senza* script non deve comparire: il filtro è sulla
    # presenza dello script, non sulla modalità con cui è stato generato.
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [con_script["id"]]


def test_mode_script_include_gli_script_generati_in_altre_modalita(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
) -> None:
    """Il filtro guarda la colonna, non `analysis_mode`.

    Se un giorno uno script venisse prodotto anche fuori dalla modalità BOTH,
    dovrebbe comparire lo stesso — è la differenza fra filtrare sul dato e
    filtrare su come il dato è stato ottenuto.
    """
    anomalo = _seed_insight(
        store, user_id=USER_ID, video_url="https://tiktok.com/v/9", mode="INFO", has_script=True
    )

    response = client.get("/api/v1/insights?mode=SCRIPT", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [anomalo["id"]]


@pytest.mark.parametrize("mode", ["INFO", "STYLE", "BOTH"])
def test_gli_altri_filtri_restano_sulla_modalita(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore, mode: str
) -> None:
    for index, seeded_mode in enumerate(("INFO", "STYLE", "BOTH")):
        _seed_insight(
            store,
            user_id=USER_ID,
            video_url=f"https://tiktok.com/v/{index}",
            mode=seeded_mode,
            has_script=seeded_mode == "BOTH",
        )

    response = client.get(f"/api/v1/insights?mode={mode}", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["analysis_mode"] == mode


def test_insights_non_espone_i_record_di_altri_utenti(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
) -> None:
    _seed_insight(
        store, user_id=USER_ID, video_url="https://tiktok.com/v/mio", mode="BOTH", has_script=True
    )
    _seed_insight(
        store,
        user_id="99999999-9999-4999-8999-999999999999",
        video_url="https://tiktok.com/v/altrui",
        mode="BOTH",
        has_script=True,
    )

    response = client.get("/api/v1/insights", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["video_url"] == "https://tiktok.com/v/mio"


# =============================================================================
# Casi limite
# =============================================================================


def test_video_troppo_grande_restituisce_422(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oltre il limite è un input rifiutato (422), non un guasto (503).

    E soprattutto: niente riga su `insights`. Un record parziale sarebbe peggio
    dell'errore, perché la cache lo restituirebbe per sempre al posto di una
    nuova analisi.
    """
    import app.services.analysis_service as analysis_module

    # Funzione sincrona: `async with download_to_temp(...)` valuta prima la
    # chiamata, quindi l'eccezione parte esattamente dove partirebbe quella vera
    # (il controllo sulla dimensione, prima di aprire lo stream).
    def oversized(url: str, settings: Any, *, known_duration_seconds: float | None = None):
        raise VideoTooLargeError("Il video pesa 512 MB e supera il limite di 200 MB.")

    monkeypatch.setattr(analysis_module, "download_to_temp", oversized)

    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": VIDEO_URL, "analysis_mode": "BOTH"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "video_too_large"
    assert store.count("insights") == 0
    assert gemini.calls == []


def test_gemini_non_disponibile_restituisce_503_senza_scrivere(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    downloads: list[str],
) -> None:
    gemini.error = GeminiError()

    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": VIDEO_URL, "analysis_mode": "BOTH"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gemini_unavailable"
    # Nessun record: un insight vuoto verrebbe servito dalla cache al prossimo
    # tentativo, rendendo il fallimento permanente.
    assert store.count("insights") == 0


def test_apify_non_raggiungibile_non_blocca_l_analisi(
    client: TestClient,
    auth_headers: dict[str, str],
    apify: FakeApify,
    downloads: list[str],
    store: FakeStore,
) -> None:
    """Apify è best effort: serve l'URL diretto, non è indispensabile.

    In rate limit `resolve_video` restituisce `None` e la pipeline ripiega
    sull'URL originale — che per un link diretto a un `.mp4` funziona.
    """
    apify.resolved = None

    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": VIDEO_URL, "analysis_mode": "BOTH"},
    )

    assert response.status_code == 201, response.text
    assert store.count("insights") == 1
    # Ha scaricato dall'URL originale — nella sua forma normalizzata, che è
    # quella con cui l'URL viaggia da `normalize_video_url` in poi.
    assert downloads == ["https://tiktok.com/@creator/video/123"]
    assert store.rows("insights")[0]["thumbnail_url"] is None


def test_url_non_valido_restituisce_422(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": "ftp://example.com/video.mp4", "analysis_mode": "BOTH"},
    )

    assert response.status_code == 422
    assert store.count("insights") == 0


def test_body_con_campi_estranei_viene_rifiutato(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """`user_id` nel body non deve poter influenzare nulla: `extra="forbid"`."""
    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={
            "video_url": VIDEO_URL,
            "analysis_mode": "BOTH",
            "user_id": "99999999-9999-4999-8999-999999999999",
        },
    )

    assert response.status_code == 422
    assert store.count("insights") == 0


# =============================================================================
# Cron: resilienza per singolo creator
# =============================================================================


def test_cron_prosegue_quando_apify_va_in_rate_limit(
    client: TestClient,
    cron_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Un creator in errore non deve azzerare il giro degli altri."""
    store.seed(
        "creators",
        {
            "user_id": USER_ID,
            "username": "creator_ko",
            "platform": "tiktok",
            "analysis_mode": "BOTH",
            "is_active": True,
        },
    )
    apify.fetch_error = ApifyError()

    response = client.post("/api/v1/cron/check-updates", headers=cron_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checked_creators"] == 1
    assert body["failed_creators"] == 1
    assert body["queued_analyses"] == 0
    assert body["results"][0]["status"] == "failed"
    # Il motivo è sintetico: nessuno stack trace, nessun dettaglio dell'actor.
    assert "Traceback" not in body["results"][0]["error"]


def test_cron_ignora_i_creator_in_pausa(
    client: TestClient,
    cron_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    store.seed(
        "creators",
        {
            "user_id": USER_ID,
            "username": "in_pausa",
            "platform": "tiktok",
            "analysis_mode": "BOTH",
            "is_active": False,
        },
    )

    response = client.post("/api/v1/cron/check-updates", headers=cron_headers)

    assert response.status_code == 200, response.text
    assert response.json()["checked_creators"] == 0
    assert apify.fetch_calls == []
