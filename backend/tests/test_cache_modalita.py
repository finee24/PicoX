"""La cache deve tenere conto della modalità richiesta, non solo dell'URL.

`UNIQUE (user_id, video_url)` ammette **una sola riga per video**, quindi la
modalità con cui quella riga è stata prodotta è parte della sua identità. Prima
di questa correzione `find_cached_insight` filtrava solo su `(user_id,
video_url)`: chi aveva analizzato un video in `INFO` e poi lo richiedeva in
`BOTH` riceveva `200` con la riga `INFO`, cioè senza `style_data` né
`inverse_script_template`, senza errore e senza modo di accorgersene.

Il confronto è per *copertura* e non per uguaglianza: una riga `BOTH` soddisfa
una richiesta `INFO`. L'uguaglianza secca sarebbe una regressione peggiore —
l'upsert successivo degraderebbe la riga, buttando via analisi già pagate.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import FakeApify, FakeGemini
from tests.fake_supabase import FakeStore

VIDEO_URL = "https://www.tiktok.com/@creator/video/999"


def _analizza(client: TestClient, headers: dict[str, str], mode: str):
    return client.post(
        "/api/v1/analyze-video",
        headers=headers,
        json={"video_url": VIDEO_URL, "analysis_mode": mode},
    )


# =============================================================================
# La riga archiviata non copre la richiesta -> si rianalizza
# =============================================================================


def test_una_riga_info_non_soddisfa_una_richiesta_both(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
) -> None:
    prima = _analizza(client, auth_headers, "INFO")
    assert prima.status_code == 201, prima.text
    assert prima.json()["style_data"] is None

    chiamate_dopo_la_prima = len(gemini.calls)

    seconda = _analizza(client, auth_headers, "BOTH")

    # 201: è stata prodotta un'analisi nuova, non riciclata quella parziale.
    assert seconda.status_code == 201, seconda.text
    assert len(gemini.calls) == chiamate_dopo_la_prima + 1

    corpo = seconda.json()
    assert corpo["analysis_mode"] == "BOTH"
    assert corpo["summary_data"] is not None
    assert corpo["style_data"] is not None
    assert corpo["inverse_script_template"] is not None

    # Il vincolo UNIQUE regge: la riga è stata aggiornata, non duplicata.
    assert store.count("insights") == 1


def test_una_riga_style_non_soddisfa_una_richiesta_info(
    client: TestClient,
    auth_headers: dict[str, str],
    gemini: FakeGemini,
) -> None:
    """`STYLE` e `INFO` non si coprono a vicenda: sono insiemi disgiunti."""
    prima = _analizza(client, auth_headers, "STYLE")
    assert prima.status_code == 201, prima.text

    chiamate = len(gemini.calls)
    seconda = _analizza(client, auth_headers, "INFO")

    assert seconda.status_code == 201, seconda.text
    assert len(gemini.calls) == chiamate + 1
    assert seconda.json()["summary_data"] is not None


# =============================================================================
# La riga archiviata copre la richiesta -> cache hit, nessuna spesa
# =============================================================================


def test_una_riga_both_soddisfa_una_richiesta_info_senza_rianalizzare(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
) -> None:
    """Il caso che una semplice uguaglianza sulla modalità avrebbe rotto.

    Rianalizzare qui non solo sprecherebbe un'inferenza: l'upsert riscriverebbe
    la riga in `INFO`, cancellando stile e script inverso già pagati.
    """
    prima = _analizza(client, auth_headers, "BOTH")
    assert prima.status_code == 201, prima.text

    chiamate = len(gemini.calls)
    risoluzioni = len(apify.resolve_calls)

    seconda = _analizza(client, auth_headers, "INFO")

    assert seconda.status_code == 200, seconda.text
    assert seconda.json()["id"] == prima.json()["id"]
    # Nessuna chiamata esterna: è il punto dell'intera cache.
    assert len(gemini.calls) == chiamate
    assert len(apify.resolve_calls) == risoluzioni

    # E soprattutto: la riga non è stata degradata.
    assert seconda.json()["style_data"] is not None
    assert seconda.json()["inverse_script_template"] is not None
    assert store.count("insights") == 1


def test_stessa_modalita_resta_un_cache_hit(
    client: TestClient,
    auth_headers: dict[str, str],
    gemini: FakeGemini,
) -> None:
    """Non-regressione del comportamento che già funzionava."""
    prima = _analizza(client, auth_headers, "BOTH")
    assert prima.status_code == 201, prima.text

    chiamate = len(gemini.calls)
    seconda = _analizza(client, auth_headers, "BOTH")

    assert seconda.status_code == 200, seconda.text
    assert len(gemini.calls) == chiamate
