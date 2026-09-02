"""Attribuzione automatica del creator sul percorso manuale.

`analyze-video` non riceve un `creator_id`: prima di questa modifica ogni
analisi fatta a mano restava orfana, anche quando l'autore del video era già
nella watchlist di chi la chiedeva. Qui si verifica che l'aggancio avvenga
quando deve e — cosa che conta uguale — che **non** avvenga quando non deve.

Ogni test che afferma "attribuito" ha accanto il suo falsificatore: senza, un
`creator_id` valorizzato per un'altra ragione renderebbe il file verde senza
verificare nulla.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.apify_service import ScrapedVideo
from tests.conftest import OTHER_USER_ID, USER_ID, FakeApify, FakeYouTube
from tests.fake_supabase import FakeStore

VIDEO_URL = "https://www.tiktok.com/@creator/video/123"
YOUTUBE_URL = "https://www.youtube.com/shorts/dQw4w9WgXcQ"


def _video_di(autore: str | None) -> ScrapedVideo:
    """Il risultato Apify per `VIDEO_URL`, con l'autore che serve al test."""
    return ScrapedVideo(
        video_url=VIDEO_URL,
        download_url="https://cdn.example.com/video-123.mp4",
        thumbnail_url="https://cdn.example.com/thumb-123.jpg",
        duration_seconds=42.0,
        author_username=autore,
    )


def _analizza(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    risposta = client.post(
        "/api/v1/analyze-video",
        headers=headers,
        json={"video_url": VIDEO_URL, "analysis_mode": "BOTH"},
    )
    assert risposta.status_code == 201, risposta.text
    corpo: dict[str, Any] = risposta.json()
    return corpo


def _segui(store: FakeStore, **campi: Any) -> dict[str, Any]:
    riga: dict[str, Any] = {
        "user_id": USER_ID,
        "username": "creator",
        "platform": "tiktok",
        "analysis_mode": "BOTH",
        "is_active": True,
    }
    riga.update(campi)
    return store.seed("creators", riga)


# =============================================================================
# 1. Il caso che la funzionalità esiste per coprire
# =============================================================================


def test_analisi_manuale_aggancia_il_creator_seguito(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    creator = _segui(store)
    apify.resolved = _video_di("creator")

    corpo = _analizza(client, auth_headers)

    assert corpo["creator_id"] == creator["id"]
    assert store.rows("insights")[0]["creator_id"] == creator["id"]


def test_senza_creator_seguito_l_insight_resta_orfano(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Il falsificatore del test qui sopra.

    Stessa richiesta, stesso autore, watchlist vuota: se anche qui comparisse un
    `creator_id`, il test precedente non proverebbe l'aggancio ma un valore che
    si presenta comunque.
    """
    apify.resolved = _video_di("creator")

    corpo = _analizza(client, auth_headers)

    assert corpo["creator_id"] is None
    assert store.rows("insights")[0]["creator_id"] is None


# =============================================================================
# 2. Quando l'aggancio non deve avvenire
# =============================================================================


def test_creator_seguito_su_un_altra_piattaforma_non_viene_agganciato(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Stesso handle, piattaforma diversa: non è lo stesso creator."""
    _segui(store, platform="instagram")
    apify.resolved = _video_di("creator")

    assert _analizza(client, auth_headers)["creator_id"] is None


def test_creator_disattivato_non_viene_agganciato(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """`is_active=False` è il soft-switch del monitoraggio: vale anche qui."""
    _segui(store, is_active=False)
    apify.resolved = _video_di("creator")

    assert _analizza(client, auth_headers)["creator_id"] is None


def test_il_creator_di_un_altro_utente_non_viene_agganciato(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Handle e piattaforma coincidono, il proprietario no.

    Il doppio di Supabase applica lui stesso lo scoping: se la query perdesse il
    filtro sul proprietario, questo test fallirebbe invece di passare.
    """
    _segui(store, user_id=OTHER_USER_ID)
    apify.resolved = _video_di("creator")

    assert _analizza(client, auth_headers)["creator_id"] is None


@pytest.mark.parametrize("autore", [None, "", "   "])
def test_autore_assente_non_aggancia_nulla(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
    autore: str | None,
) -> None:
    _segui(store)
    apify.resolved = _video_di(autore)

    assert _analizza(client, auth_headers)["creator_id"] is None


def test_un_titolo_di_canale_non_viene_scambiato_per_un_handle(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Sul percorso YouTube-API `author_username` è il **nome** del canale.

    `clean_username` fa da guardia: un titolo con spazi non è un handle valido,
    quindi non si accoppia a nulla. Senza quella validazione un confronto più
    permissivo potrebbe attribuire il video al creator sbagliato.
    """
    _segui(store)
    apify.resolved = _video_di("Creator Ufficiale")

    assert _analizza(client, auth_headers)["creator_id"] is None


# =============================================================================
# 3. Forma dell'handle
# =============================================================================


@pytest.mark.parametrize(
    "seguito,scrapato",
    [("Creator", "creator"), ("creator", "Creator"), ("creator", "@creator")],
)
def test_il_confronto_ignora_maiuscole_e_chiocciola(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
    seguito: str,
    scrapato: str,
) -> None:
    """Gli handle sono case-insensitive sulle tre piattaforme.

    `creators.username` conserva invece le maiuscole scritte dall'utente: un
    confronto esatto terrebbe separati due nomi dello stesso creator.
    """
    creator = _segui(store, username=seguito)
    apify.resolved = _video_di(scrapato)

    assert _analizza(client, auth_headers)["creator_id"] == creator["id"]


def test_un_handle_simile_non_basta(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Il falsificatore del confronto permissivo.

    `creator_x` e `creator-x` differiscono per un carattere che in una `LIKE`
    sarebbe un metacarattere (`_` sta per un carattere qualsiasi): con un
    `ilike` al posto del confronto in Python, questo test attribuirebbe il video
    al creator sbagliato.
    """
    _segui(store, username="creator_x")
    apify.resolved = _video_di("creator-x")

    assert _analizza(client, auth_headers)["creator_id"] is None


# =============================================================================
# 4. YouTube: il titolo del canale non e' un handle, e lo sceglie un terzo
# =============================================================================


def test_su_youtube_un_titolo_handle_shaped_non_aggancia_nulla(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    youtube: FakeYouTube,
) -> None:
    """Il caso che `clean_username` da sola non copre.

    Su YouTube `author_username` e' il **titolo** del canale (`videos.list` non
    espone l'handle), e il titolo lo sceglie chi possiede il canale. Un titolo
    di una parola sola alfanumerica e' un handle *sintatticamente* valido: la
    guardia lo lascia passare, perche' sta verificando la forma, non l'identita'.

    Chiunque puo' quindi intitolare il proprio canale come un creator che la
    vittima segue e farsi attribuire il video nella watchlist altrui. La
    piattaforma va esclusa finche' non c'e' un handle vero da confrontare.
    """
    _segui(store, username="geopop", platform="youtube_shorts")
    assert youtube.video is not None
    # Non "Creator Ufficiale": un titolo che *sembra* un handle, cioe' il caso
    # che supera la validazione sintattica.
    youtube.video.channel_title = "geopop"

    risposta = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": YOUTUBE_URL, "analysis_mode": "STYLE"},
    )

    assert risposta.status_code == 201, risposta.text
    assert risposta.json()["creator_id"] is None, (
        "il titolo del canale e' stato scambiato per l'handle: su youtube_shorts "
        "la deduzione non deve avvenire affatto"
    )
    assert store.rows("insights")[0]["creator_id"] is None



def test_su_youtube_nemmeno_il_ripiego_apify_aggancia(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
    youtube: FakeYouTube,
) -> None:
    """Il secondo vettore YouTube, che il test qui sopra non tocca.

    Quando la YouTube API non restituisce metadati si ripiega su Apify
    (`content_scraper`), ma `author_username` non migliora: `_AUTHOR_KEYS`
    prova `channelUsername` e poi **ricade** su `channelName`/`channelTitle`,
    che sono nomi visualizzati scelti dal proprietario del canale.

    La `platform` resta `youtube_shorts` anche su questo ramo, quindi
    l'esclusione lo copre — ma senza questo test la copertura riguarderebbe il
    solo passthrough, e un domani il ripiego potrebbe divergere senza che nulla
    lo segnali.
    """
    _segui(store, username="geopop", platform="youtube_shorts")
    # Nessun metadato dalla Data API: e' cio' che forza il ripiego su Apify.
    youtube.video = None
    apify.resolved = _video_di("geopop")

    risposta = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": YOUTUBE_URL, "analysis_mode": "STYLE"},
    )

    assert risposta.status_code == 201, risposta.text
    assert risposta.json()["creator_id"] is None, (
        "il ripiego Apify su YouTube ha agganciato un creator: la platform e' "
        "ancora youtube_shorts e l'esclusione deve valere anche qui"
    )


def test_la_stessa_corrispondenza_aggancia_su_tiktok(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Il falsificatore dell'esclusione YouTube.

    Handle identico, corrispondenza identica, watchlist identica: cambia solo
    la piattaforma. Se anche questo restasse orfano, il test qui sopra non
    proverebbe l'esclusione di `youtube_shorts` ma un aggancio rotto ovunque.
    """
    creator = _segui(store, username="geopop", platform="tiktok")
    apify.resolved = _video_di("geopop")

    assert _analizza(client, auth_headers)["creator_id"] == creator["id"]


# =============================================================================
# 5. Una riga di `creators` malformata non e' un 500
#
# (Che il creator dedotto non sostituisca quello in archivio si verifica in
# `test_concorrenza_analisi.py`: la' c'e' gia' l'apparato per far scrivere il
# cron prima dell'analisi manuale.)
# =============================================================================


def test_una_riga_di_creators_senza_id_non_produce_un_500(
    client: TestClient,
    auth_headers: dict[str, str],
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """`data` di PostgREST e' JSON generico: si legge con `.get`, ovunque.

    Le due letture di `username` nello stesso blocco sono gia' difensive. Con
    `scelto["id"]` una riga priva della chiave diventava un `KeyError`, cioe' un
    500 su un'analisi **gia' pagata**, invece di una semplice mancata
    attribuzione.
    """
    riga = _segui(store)
    del riga["id"]
    apify.resolved = _video_di("creator")

    risposta = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": VIDEO_URL, "analysis_mode": "BOTH"},
    )

    assert risposta.status_code == 201, risposta.text
    assert risposta.json()["creator_id"] is None
