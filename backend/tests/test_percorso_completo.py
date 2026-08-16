"""Le giunture fra i quattro pezzi, percorse tutte insieme.

I test per materia provano ciascun pezzo dove vive: la validazione in
`test_validazione_creator`, gli scraper in `test_scraper_contenuti`, il prompt in
`test_prompt_analisi`. Restano fuori le **giunture**, che sono di nessuno e per
questo si rompono senza far fallire nulla:

1. `POST /creators/validate` restituisce un `normalized_identifier` che
   `POST /creators` deve accettare. Sono due endpoint scritti in momenti diversi
   con due normalizzazioni diverse, ed e' esattamente il punto in cui la card di
   anteprima puo' mostrare un account valido che poi non si riesce a seguire.
2. Caption e autore raccolti dallo scraper devono arrivare fino al prompt. In
   mezzo ci sono due percorsi (passthrough e byte) e due chiamanti (manuale e
   cron): un anello rotto non si vede da nessuna delle due estremita'.
3. Le due quote — analisi e validazioni — contano su tabelle diverse. Se si
   confondessero, validare consumerebbe il diritto di analizzare.

NOTA SUI LIMITI. `FakeGemini` sostituisce il livello in cui `fps` e
`media_resolution` vengono applicati: qui si osserva che la **modalita'** arriva
fino alla chiamata, mentre il preset che ne deriva e' verificato direttamente in
`test_scraper_contenuti`. Nessun test di questa suite tocca la rete.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import (
    OTHER_USER_ID,
    TEST_CRON_SECRET,
    USER_ID,
    FakeApify,
    FakeGemini,
    FakeYouTube,
)
from tests.fake_supabase import FakeStore

CRON_HEADERS = {"X-CRON-SECRET": TEST_CRON_SECRET}

INSTAGRAM_URL = "https://www.instagram.com/reel/Cxyz12345/"
YOUTUBE_URL = "https://www.youtube.com/shorts/dQw4w9WgXcQ"


def _valida(client: TestClient, headers: dict[str, str], testo: str):
    return client.post("/api/v1/creators/validate", headers=headers, json={"input": testo})


def _segui(client: TestClient, headers: dict[str, str], username: str, platform: str, mode: str):
    """Quello che fa il bottone Segui: stesso endpoint della pagina Creator."""
    return client.post(
        "/api/v1/creators",
        headers=headers,
        json={"username": username, "platform": platform, "analysis_mode": mode},
    )


# =============================================================================
# 1. Dalla verifica al creator monitorato
# =============================================================================


def test_l_identificatore_verificato_e_accettato_da_post_creators(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """LA GIUNTURA CHE LA CARD DI ANTEPRIMA ATTRAVERSA.

    La card mostra un profilo verificato e offre di seguirlo: se il valore che
    `/validate` normalizza non fosse quello che `POST /creators` accetta,
    l'utente vedrebbe un account valido e un bottone che risponde 422. Le due
    normalizzazioni vivono in file diversi e nulla, a parte questo test, le
    tiene allineate.
    """
    verifica = _valida(client, auth_headers, "https://www.instagram.com/@Creator/")

    assert verifica.status_code == 200, verifica.text
    corpo = verifica.json()
    assert corpo["exists"] is True and corpo["is_public"] is True

    seguito = _segui(
        client, auth_headers, corpo["normalized_identifier"], corpo["platform"], "INFO"
    )

    assert seguito.status_code == 201, seguito.text
    righe = store.rows("creators")
    assert len(righe) == 1
    assert righe[0]["username"] == corpo["normalized_identifier"]
    assert righe[0]["platform"] == corpo["platform"]


def test_seguire_due_volte_dalla_card_resta_un_conflitto(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """La card e' una scorciatoia, non un flusso parallelo.

    Passa dallo stesso endpoint e prende quindi lo stesso 409 della pagina
    "Creator monitorati": un secondo Segui non crea un duplicato.
    """
    corpo = _valida(client, auth_headers, "instagram.com/creator").json()
    identificatore = corpo["normalized_identifier"]

    assert _segui(client, auth_headers, identificatore, "instagram", "INFO").status_code == 201
    assert _segui(client, auth_headers, identificatore, "instagram", "INFO").status_code == 409


# =============================================================================
# 2. Dal creator seguito all'insight, passando per prompt e scraper
# =============================================================================


def test_catena_completa_su_youtube(
    client: TestClient,
    auth_headers: dict[str, str],
    youtube: FakeYouTube,
    apify: FakeApify,
    gemini: FakeGemini,
    store: FakeStore,
    downloads: list[str],
) -> None:
    """I quattro pezzi in fila su un unico video.

    Verifica del canale (Data API) -> creator monitorato -> giro di cron ->
    scraping -> passthrough a Gemini -> prompt con il contesto -> insight
    attribuito. Ogni anello e' gia' testato da solo; qui conta che siano
    attaccati.
    """
    from app.services.apify_service import ScrapedVideo

    corpo = _valida(client, auth_headers, "https://www.youtube.com/@Creator").json()
    assert corpo["platform"] == "youtube_shorts"
    assert _segui(
        client, auth_headers, corpo["normalized_identifier"], "youtube_shorts", "INFO"
    ).status_code == 201

    apify.videos = [
        ScrapedVideo(
            video_url=YOUTUBE_URL,
            download_url="https://cdn.example.com/short.mp4",
            duration_seconds=38.0,
            caption="Come studiare la sera #studio #metodo",
            author_username="creator",
        )
    ]

    giro = client.post("/api/v1/cron/check-updates", headers=CRON_HEADERS)

    assert giro.status_code == 200, giro.text
    assert giro.json()["queued_analyses"] == 1

    # Il video e' passato per URL: nessun byte scaricato, nessun upload.
    assert len(gemini.url_calls) == 1, "il passthrough non e' stato usato dal cron"
    assert gemini.calls == [], "il cron ha scaricato un video YouTube"
    assert downloads == []
    assert gemini.url_calls[0][1] == "INFO", "la modalita' del creator non e' arrivata"

    # Il contesto e' arrivato con il video, ed e' quello dello scraping.
    contesto = gemini.contexts[-1]
    assert contesto is not None
    assert contesto.platform == "youtube_shorts"
    assert contesto.caption == "Come studiare la sera #studio #metodo"
    assert contesto.hashtags == ["studio", "metodo"]

    # L'insight esiste, e' dell'utente ed e' attribuito al creator.
    insight = store.rows("insights")
    assert len(insight) == 1
    assert insight[0]["user_id"] == USER_ID
    assert insight[0]["analysis_mode"] == "INFO"
    assert insight[0]["creator_id"] == store.rows("creators")[0]["id"]


def test_catena_completa_su_instagram(
    client: TestClient,
    auth_headers: dict[str, str],
    apify: FakeApify,
    gemini: FakeGemini,
    store: FakeStore,
    downloads: list[str],
) -> None:
    """Stessa catena sull'altro percorso: byte scaricati, non URL.

    Instagram non ha un passthrough nativo, quindi qui il video si scarica
    davvero — ed e' il caso in cui un contesto perso sarebbe passato inosservato,
    perche' la parte visibile dell'analisi funziona lo stesso.
    """
    from app.services.apify_service import ScrapedVideo

    corpo = _valida(client, auth_headers, "instagram.com/creator").json()
    assert _segui(
        client, auth_headers, corpo["normalized_identifier"], "instagram", "BOTH"
    ).status_code == 201

    apify.videos = [
        ScrapedVideo(
            video_url=INSTAGRAM_URL,
            download_url="https://cdn.example.com/reel.mp4",
            duration_seconds=25.0,
            caption="Tre errori in cucina #cucina #ricette",
            author_username="chef",
        )
    ]

    giro = client.post("/api/v1/cron/check-updates", headers=CRON_HEADERS)

    assert giro.status_code == 200, giro.text
    assert gemini.url_calls == [], "un Reel non si passa per URL"
    assert len(gemini.calls) == 1
    assert downloads == ["https://cdn.example.com/reel.mp4"]

    contesto = gemini.contexts[-1]
    assert contesto is not None
    assert contesto.platform == "instagram"
    assert contesto.creator_username == "@chef", "sul Reel l'autore e' un handle"
    assert contesto.hashtags == ["cucina", "ricette"]
    assert len(store.rows("insights")) == 1


def test_sul_passthrough_manuale_il_contesto_dice_che_e_un_titolo(
    client: TestClient,
    auth_headers: dict[str, str],
    youtube: FakeYouTube,
    gemini: FakeGemini,
) -> None:
    """L'asimmetria che il percorso YouTube introduce, osservata dove nasce.

    `videos.list` non espone una caption ne' un handle: nel contesto finiscono
    il **titolo** del video e il **nome** del canale. Se un giorno qualcuno
    riallineasse i due percorsi "per coerenza", l'autore diventerebbe
    `@Creator Ufficiale` — un riferimento che non esiste su nessuna piattaforma.
    """
    risposta = client.post(
        "/api/v1/analyze-video",
        headers=auth_headers,
        json={"video_url": YOUTUBE_URL, "analysis_mode": "STYLE"},
    )

    assert risposta.status_code == 201, risposta.text
    assert len(gemini.url_calls) == 1

    contesto = gemini.contexts[-1]
    assert contesto is not None
    assert youtube.video is not None
    assert contesto.caption == youtube.video.title
    assert contesto.creator_username == youtube.video.channel_title
    assert not contesto.creator_username.startswith("@")


# =============================================================================
# 3. Le due quote non si toccano
# =============================================================================


def test_validare_non_consuma_la_quota_di_analisi(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """Due tetti, due tabelle, due trigger (`PX003` e `PX002`).

    Sono meccanismi gemelli e per questo confondibili: se le verifiche
    finissero in `analysis_events`, un utente che valuta dieci account prima di
    sceglierne uno si troverebbe senza analisi senza aver analizzato nulla.
    """
    for handle in ("creator", "altrocreator", "terzocreator"):
        assert _valida(client, auth_headers, f"instagram.com/{handle}").status_code == 200

    assert len(store.rows("validation_events")) == 3
    assert store.rows("analysis_events") == []


def test_analizzare_non_consuma_la_quota_di_validazione(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    assert (
        client.post(
            "/api/v1/analyze-video",
            headers=auth_headers,
            json={"video_url": INSTAGRAM_URL, "analysis_mode": "INFO"},
        ).status_code
        == 201
    )

    assert len(store.rows("analysis_events")) == 1
    assert store.rows("validation_events") == []


def test_il_cron_non_consuma_la_quota_dell_utente(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    """Il tetto giornaliero e' sulle analisi *richieste*, non su quelle ricevute.

    Un utente che si sveglia senza quota perche' stanotte e' girato il cron non
    saprebbe spiegarsi il limite — e la giuntura fra creator monitorati e quota
    e' proprio dove quella confusione si creerebbe.
    """
    from app.services.apify_service import ScrapedVideo

    corpo = _valida(client, auth_headers, "instagram.com/creator").json()
    _segui(client, auth_headers, corpo["normalized_identifier"], "instagram", "INFO")

    apify.videos = [
        ScrapedVideo(
            video_url=INSTAGRAM_URL,
            download_url="https://cdn.example.com/reel.mp4",
            duration_seconds=25.0,
        )
    ]

    assert client.post("/api/v1/cron/check-updates", headers=CRON_HEADERS).status_code == 200

    assert len(store.rows("insights")) == 1, "il cron non ha analizzato nulla"
    assert store.rows("analysis_events") == [], "il cron ha eroso la quota dell'utente"


# =============================================================================
# 4. La cache della validazione e' condivisa, la quota no
# =============================================================================


def test_la_seconda_verifica_dello_stesso_account_non_ripaga_il_provider(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    """La cache e' globale, la quota resta per utente.

    Sono due decisioni opposte sullo stesso dato e vale la pena vederle insieme:
    il profilo pubblico e' uguale per tutti (una cache per utente moltiplicherebbe
    la spesa esterna), ma il diritto di *chiedere* e' individuale — altrimenti
    basterebbe un account per svuotare il budget di tutti.
    """
    assert _valida(client, auth_headers, "instagram.com/creator").status_code == 200
    assert _valida(client, auth_headers, "instagram.com/creator").status_code == 200

    assert len(apify.profile_calls) == 1, "il provider e' stato pagato due volte"
    assert len(store.rows("creator_validations")) == 1
    assert len(store.rows("validation_events")) == 1, "un hit di cache non consuma quota"


# =============================================================================
# 5. Il cron non deve vedere gli insight di un altro tenant
# =============================================================================


def test_il_cron_analizza_per_me_un_video_che_un_altro_utente_ha_gia_analizzato(
    client: TestClient,
    auth_headers: dict[str, str],
    other_auth_headers: dict[str, str],
    apify: FakeApify,
    store: FakeStore,
) -> None:
    """Il caso che mancava, ed e' il percorso reale e non il builder isolato.

    Il cron **non ha un JWT** e ricade sul client service-role, dove il RLS non
    esiste: l'unica cosa che separa i tenant e' il filtro che `ScopedTable`
    applica da se'. Due punti del giro lo attraversano —
    `_filter_already_analyzed`, che chiede quali video sono gia' stati
    analizzati, e `find_cached_insight`, che rilegge la riga — ed entrambi
    leggono `insights`.

    Senza quel filtro il video risulterebbe gia' analizzato **perche' lo ha
    analizzato qualcun altro**, il cron lo salterebbe, e l'utente non
    riceverebbe mai il proprio insight. Nessun errore, nessun log: solo un feed
    che non si popola.

    Gli altri test del cron usano un solo tenant, e senza righe altrui un filtro
    mancante non ha nulla da rivelare: e' il motivo per cui la suite restava
    verde con la protezione spenta.
    """
    from app.services.apify_service import ScrapedVideo

    # Un altro utente ha gia' analizzato questo video, per conto suo.
    assert (
        client.post(
            "/api/v1/analyze-video",
            headers=other_auth_headers,
            json={"video_url": INSTAGRAM_URL, "analysis_mode": "INFO"},
        ).status_code
        == 201
    )
    assert {riga["user_id"] for riga in store.rows("insights")} == {
        OTHER_USER_ID
    }, "presupposto del test non valido"

    # Io seguo un creator il cui feed contiene lo stesso video.
    corpo = _valida(client, auth_headers, "instagram.com/creator").json()
    _segui(client, auth_headers, corpo["normalized_identifier"], "instagram", "INFO")
    apify.videos = [
        ScrapedVideo(
            video_url=INSTAGRAM_URL,
            download_url="https://cdn.example.com/reel.mp4",
            duration_seconds=25.0,
        )
    ]

    assert client.post("/api/v1/cron/check-updates", headers=CRON_HEADERS).status_code == 200

    miei = [riga for riga in store.rows("insights") if riga["user_id"] == USER_ID]
    assert len(miei) == 1, (
        "il cron ha saltato il video perche' lo ha visto analizzato da un altro "
        "utente: il filtro sul proprietario non e' stato applicato"
    )
    assert len(store.rows("insights")) == 2, "le due righe devono restare distinte"
