"""Contratto fra backend e frontend, reso eseguibile.

`frontend/lib/types.ts` è scritto a mano: non c'è generazione automatica dagli
schemi Pydantic, quindi nulla impedisce che i due lati divergano in silenzio
finché qualcuno non apre la dashboard. Questi test fissano ciò che il frontend
legge davvero — nomi dei campi, status code, forma dell'envelope d'errore — così
che una divergenza si manifesti in CI e non in produzione.

Ogni insieme di chiavi qui sotto ha un'interfaccia corrispondente in
`frontend/lib/types.ts`: cambiando l'uno va cambiato l'altro.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import USER_ID
from tests.fake_supabase import FakeStore

# `Insight` in frontend/lib/types.ts
CAMPI_INSIGHT = {
    "id",
    "creator_id",
    "user_id",
    "video_url",
    "thumbnail_url",
    "analysis_mode",
    "summary_data",
    "style_data",
    "inverse_script_template",
    "keywords",
    "created_at",
}

# `Creator` in frontend/lib/types.ts
CAMPI_CREATOR = {
    "id",
    "user_id",
    "username",
    "platform",
    "analysis_mode",
    "is_active",
    "created_at",
    "updated_at",
}

# `InsightListResponse` in frontend/lib/types.ts
CAMPI_PAGINA = {"items", "page", "limit", "total", "has_more"}


def _crea_creator(client: TestClient, headers: dict[str, str], **overrides) -> dict:
    payload = {"username": "geopop", "platform": "tiktok", "analysis_mode": "BOTH"}
    payload.update(overrides)
    response = client.post("/api/v1/creators", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# =============================================================================
# Forma delle risposte
# =============================================================================


def test_insight_espone_esattamente_i_campi_attesi_dal_frontend(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    response = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": "https://www.tiktok.com/@c/video/1", "analysis_mode": "BOTH"},
    )

    assert response.status_code == 201, response.text
    assert set(response.json()) == CAMPI_INSIGHT


def test_la_lista_insight_ha_la_forma_paginata_attesa(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/api/v1/insights", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == CAMPI_PAGINA
    assert body["items"] == []
    assert body["total"] == 0
    assert body["has_more"] is False


def test_creator_espone_esattamente_i_campi_attesi_dal_frontend(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    creato = _crea_creator(client, auth_headers)

    assert set(creato) == CAMPI_CREATOR

    elenco = client.get("/api/v1/creators", headers=auth_headers)
    assert elenco.status_code == 200
    assert set(elenco.json()) == {"items", "total"}
    assert set(elenco.json()["items"][0]) == CAMPI_CREATOR


def test_l_envelope_d_errore_e_sempre_lo_stesso(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """`frontend/lib/api.ts` legge `error.code` per decidere cosa mostrare."""
    _crea_creator(client, auth_headers)
    duplicato = client.post(
        "/api/v1/creators",
        headers=auth_headers,
        json={"username": "geopop", "platform": "tiktok", "analysis_mode": "BOTH"},
    )

    assert duplicato.status_code == 409
    body = duplicato.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "conflict"
    assert isinstance(body["error"]["message"], str)


# =============================================================================
# Status code su cui il frontend si basa
# =============================================================================


def test_status_code_del_ciclo_di_vita_di_un_creator(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    creato = _crea_creator(client, auth_headers)
    creator_id = creato["id"]

    # PATCH -> 200 con la risorsa aggiornata
    patch = client.patch(
        f"/api/v1/creators/{creator_id}", headers=auth_headers, json={"is_active": False}
    )
    assert patch.status_code == 200
    assert patch.json()["is_active"] is False

    # PATCH senza campi -> 422 (`CreatorUpdate` richiede almeno un campo)
    vuoto = client.patch(f"/api/v1/creators/{creator_id}", headers=auth_headers, json={})
    assert vuoto.status_code == 422

    # DELETE -> 204 senza corpo: `apiFetch` in api.ts non prova a fare .json()
    cancella = client.delete(f"/api/v1/creators/{creator_id}", headers=auth_headers)
    assert cancella.status_code == 204
    assert cancella.content == b""

    # DELETE ripetuta -> 404
    assert client.delete(f"/api/v1/creators/{creator_id}", headers=auth_headers).status_code == 404


def test_un_creator_di_un_altro_utente_e_invisibile_non_vietato(
    client: TestClient, auth_headers: dict[str, str], other_auth_headers: dict[str, str]
) -> None:
    """404 e non 403: l'esistenza dell'id di un altro tenant non va confermata."""
    altrui = _crea_creator(client, other_auth_headers)

    assert (
        client.patch(
            f"/api/v1/creators/{altrui['id']}", headers=auth_headers, json={"is_active": False}
        ).status_code
        == 404
    )
    cancella_altrui = client.delete(
        f"/api/v1/creators/{altrui['id']}", headers=auth_headers
    )
    assert cancella_altrui.status_code == 404

    # E non è finito nella lista dell'altro utente.
    assert client.get("/api/v1/creators", headers=auth_headers).json()["total"] == 0


# =============================================================================
# ON DELETE SET NULL — gli insight sopravvivono al creator
# =============================================================================


def test_rimuovere_un_creator_non_fa_sparire_i_suoi_insight(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """Requisito di prodotto: l'analisi è già stata pagata in token AI.

    Il vincolo che lo garantisce è `insights.creator_id ... ON DELETE SET NULL`
    nella migration 0001. Qui si verifica l'effetto osservabile dall'API: dopo la
    cancellazione l'insight è ancora nel feed, solo senza attribuzione.
    """
    creator = _crea_creator(client, auth_headers)

    store.seed(
        "insights",
        {
            "user_id": USER_ID,
            "creator_id": creator["id"],
            "video_url": "https://tiktok.com/@geopop/video/1",
            "thumbnail_url": None,
            "analysis_mode": "BOTH",
            "summary_data": {"main_topic": "aria condizionata"},
            "style_data": None,
            "inverse_script_template": {"title": "Schema"},
            "keywords": ["scienza"],
        },
    )

    prima = client.get("/api/v1/insights", headers=auth_headers).json()
    assert prima["total"] == 1
    assert prima["items"][0]["creator_id"] == creator["id"]

    cancella = client.delete(f"/api/v1/creators/{creator['id']}", headers=auth_headers)
    assert cancella.status_code == 204

    dopo = client.get("/api/v1/insights", headers=auth_headers).json()

    # L'insight c'è ancora: è il punto.
    assert dopo["total"] == 1
    insight = dopo["items"][0]
    assert insight["video_url"] == "https://tiktok.com/@geopop/video/1"
    assert insight["summary_data"] == {"main_topic": "aria condizionata"}
    # Perde solo il legame col creator, che non esiste più.
    assert insight["creator_id"] is None


def test_un_insight_orfano_resta_visibile_anche_col_filtro_script(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """La perdita del creator non deve escludere il record dai filtri."""
    creator = _crea_creator(client, auth_headers)
    store.seed(
        "insights",
        {
            "user_id": USER_ID,
            "creator_id": creator["id"],
            "video_url": "https://tiktok.com/@geopop/video/2",
            "thumbnail_url": None,
            "analysis_mode": "BOTH",
            "summary_data": None,
            "style_data": None,
            "inverse_script_template": {"title": "Schema riusabile"},
            "keywords": [],
        },
    )

    client.delete(f"/api/v1/creators/{creator['id']}", headers=auth_headers)

    con_script = client.get("/api/v1/insights?mode=SCRIPT", headers=auth_headers).json()
    assert con_script["total"] == 1
    assert con_script["items"][0]["creator_id"] is None


# =============================================================================
# Paginazione e ricerca
# =============================================================================


def _seed_molti(store: FakeStore, quanti: int) -> None:
    for index in range(quanti):
        store.seed(
            "insights",
            {
                "user_id": USER_ID,
                "creator_id": None,
                "video_url": f"https://tiktok.com/v/{index:03d}",
                "thumbnail_url": None,
                "analysis_mode": "BOTH",
                "summary_data": {"main_topic": f"argomento {index}"},
                "style_data": None,
                "inverse_script_template": None,
                "keywords": ["comune"],
            },
        )


def test_la_paginazione_e_reale_e_total_e_il_totale_filtrato(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """`has_more` guida l'infinite scroll: se mente, la dashboard si blocca."""
    _seed_molti(store, 25)

    prima = client.get("/api/v1/insights?page=1&limit=12", headers=auth_headers).json()
    assert len(prima["items"]) == 12
    assert prima["total"] == 25  # totale, non lunghezza della pagina
    assert prima["has_more"] is True

    terza = client.get("/api/v1/insights?page=3&limit=12", headers=auth_headers).json()
    assert len(terza["items"]) == 1
    assert terza["has_more"] is False

    # Nessuna sovrapposizione fra le pagine.
    seconda = client.get("/api/v1/insights?page=2&limit=12", headers=auth_headers).json()
    visti = [item["id"] for page in (prima, seconda, terza) for item in page["items"]]
    assert len(set(visti)) == 25


def test_limit_fuori_scala_viene_rifiutato(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/insights?limit=101", headers=auth_headers).status_code == 422
    assert client.get("/api/v1/insights?limit=0", headers=auth_headers).status_code == 422
    assert client.get("/api/v1/insights?page=0", headers=auth_headers).status_code == 422


def test_la_ricerca_trova_per_keyword_e_per_testo(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    store.seed(
        "insights",
        {
            "user_id": USER_ID,
            "creator_id": None,
            "video_url": "https://tiktok.com/v/a",
            "thumbnail_url": None,
            "analysis_mode": "INFO",
            "summary_data": {"main_topic": "routine di studio", "summary": "come studiare"},
            "style_data": None,
            "inverse_script_template": None,
            "keywords": ["produttività"],
        },
    )
    store.seed(
        "insights",
        {
            "user_id": USER_ID,
            "creator_id": None,
            "video_url": "https://tiktok.com/v/b",
            "thumbnail_url": None,
            "analysis_mode": "INFO",
            "summary_data": {"main_topic": "ricetta veloce", "summary": "pasta in 10 minuti"},
            "style_data": None,
            "inverse_script_template": None,
            "keywords": ["cucina"],
        },
    )

    per_keyword = client.get("/api/v1/insights?search=produttività", headers=auth_headers).json()
    assert [i["video_url"] for i in per_keyword["items"]] == ["https://tiktok.com/v/a"]

    per_testo = client.get("/api/v1/insights?search=ricetta", headers=auth_headers).json()
    assert [i["video_url"] for i in per_testo["items"]] == ["https://tiktok.com/v/b"]


def test_i_caratteri_speciali_nella_ricerca_non_rompono_la_query(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """I metacaratteri di PostgREST vanno neutralizzati, non propagati.

    `,` `(` `)` `{` `}` `*` delimitano la lista `or=(...)`: un termine che li
    contiene potrebbe altrimenti alterare la struttura del filtro.
    """
    _seed_molti(store, 3)

    for termine in ("a,b", "(x)", "{y}", "*", 'virgolette"', "back\\slash"):
        response = client.get(f"/api/v1/insights?search={termine}", headers=auth_headers)
        assert response.status_code == 200, f"{termine}: {response.text}"


def test_username_del_creator_normalizzato_e_validato(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # La '@' iniziale viene rimossa: '@nome' e 'nome' sono lo stesso creator.
    creato = _crea_creator(client, auth_headers, username="@geopop")
    assert creato["username"] == "geopop"

    invalido = client.post(
        "/api/v1/creators",
        headers=auth_headers,
        json={"username": "nome con spazi/", "platform": "tiktok", "analysis_mode": "BOTH"},
    )
    assert invalido.status_code == 422

    piattaforma_ignota = client.post(
        "/api/v1/creators",
        headers=auth_headers,
        json={"username": "tizio", "platform": "onlyfans", "analysis_mode": "BOTH"},
    )
    assert piattaforma_ignota.status_code == 422
