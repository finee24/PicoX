"""Validazione di un account creator: parsing, esiti, cache, quota.

Come per `test_quota_analisi.py`, il tetto vero vive in un trigger (migration
`0010`) e il doppio in memoria i trigger non li ha. Qui si verifica cio' che il
doppio **puo'** riprodurre fedelmente — chi consuma quota e chi no, cosa finisce
in cache, come l'errore del database diventa una risposta HTTP — mentre
l'imposizione del tetto va verificata contro il progetto reale.

LE DUE PROPRIETA' CHE CONTANO DI PIU'.

1. **Un guasto del provider non e' "account inesistente".** Sono due esiti
   diversi e vanno tenuti distinti fino alla risposta: confonderli direbbe a un
   utente che il suo canale non esiste ogni volta che Apify ha un problema.
2. **La cache non e' il rate limit.** Un hit non consuma quota perche' non
   costa nulla; ma identificatori *diversi* consumano ognuno il suo, ed e'
   quello il vettore d'abuso che il tetto chiude.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import ApifyError, ValidationQuotaError
from app.services.apify_service import ScrapedProfile
from app.services.supabase_service import translate_postgrest_error
from app.services.youtube_service import YouTubeChannel
from tests.conftest import USER_ID, FakeApify, FakeYouTube
from tests.fake_supabase import FakeAPIError, FakeStore

MESSAGGIO_POSTGRES = "Quota di 50 validazioni giornaliere raggiunta per il piano free."


def _valida(
    client: TestClient,
    headers: dict[str, str],
    valore: str,
    piattaforma: str | None = None,
):
    corpo: dict[str, Any] = {"input": valore}
    if piattaforma is not None:
        corpo["platform"] = piattaforma
    return client.post("/api/v1/creators/validate", headers=headers, json=corpo)


# =============================================================================
# 1. Riconoscimento di cio' che l'utente ha scritto
# =============================================================================


@pytest.mark.parametrize(
    ("valore", "piattaforma_attesa", "identificatore_atteso"),
    [
        ("https://www.instagram.com/creator/", "instagram", "creator"),
        ("instagram.com/creator", "instagram", "creator"),
        ("https://www.tiktok.com/@creator", "tiktok", "creator"),
        ("https://www.youtube.com/@creator", "youtube_shorts", "creator"),
        ("https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa", "youtube_shorts", "creator"),
        # Maiuscole e '@' non creano un secondo profilo: l'handle e'
        # case-insensitive su tutte e tre le piattaforme.
        ("https://instagram.com/CREATOR", "instagram", "creator"),
    ],
)
def test_la_piattaforma_si_deduce_dal_link(
    client: TestClient,
    auth_headers: dict[str, str],
    valore: str,
    piattaforma_attesa: str,
    identificatore_atteso: str,
) -> None:
    risposta = _valida(client, auth_headers, valore)

    assert risposta.status_code == 200, risposta.text
    corpo = risposta.json()
    assert corpo["platform"] == piattaforma_attesa
    assert corpo["normalized_identifier"] == identificatore_atteso


def test_dal_link_di_un_video_tiktok_si_ricava_l_autore(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """`tiktok.com/@tizio/video/123` porta con se' l'handle di chi ha pubblicato.

    E' un'informazione vera e non ha senso rifiutarla. L'asimmetria con
    `instagram.com/reel/Cxyz` — che invece e' 422 — non e' un'incoerenza: da
    quest'ultimo l'autore non e' ricavabile, quindi l'unica risposta onesta e'
    chiedere il profilo.
    """
    corpo = _valida(client, auth_headers, "https://www.tiktok.com/@creator/video/7123456789").json()

    assert corpo["platform"] == "tiktok"
    assert corpo["normalized_identifier"] == "creator"


def test_il_link_vince_sulla_piattaforma_dichiarata(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Chi incolla un link Instagram avendo "TikTok" selezionato intende Instagram.

    E' anche cio' che permette all'interfaccia di aggiornare da se' il selettore
    dopo un incolla, invece di validare la piattaforma sbagliata in silenzio.
    """
    risposta = _valida(client, auth_headers, "https://instagram.com/creator", "tiktok")

    assert risposta.status_code == 200, risposta.text
    assert risposta.json()["platform"] == "instagram"


def test_un_handle_nudo_richiede_la_piattaforma(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify
) -> None:
    """`@tizio` esiste su tutte e tre: indovinare significherebbe pagarne tre."""
    risposta = _valida(client, auth_headers, "@creator")

    assert risposta.status_code == 422, risposta.text
    assert risposta.json()["error"]["code"] == "validation_error"
    assert apify.profile_calls == [], "ha chiamato il provider su un input ambiguo"


def test_un_handle_con_piattaforma_esplicita_passa(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    risposta = _valida(client, auth_headers, "@Creator", "instagram")

    assert risposta.status_code == 200, risposta.text
    assert risposta.json()["normalized_identifier"] == "creator"


def test_youtube_e_accettato_come_sinonimo_di_youtube_shorts(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Il nome canonico resta uno solo, ma in ingresso si e' tolleranti.

    Il valore restituito e' quello che il client passera' poi a
    `POST /api/v1/creators`, dove il CHECK constraint conosce solo
    `youtube_shorts`.
    """
    risposta = _valida(client, auth_headers, "@creator", "youtube")

    assert risposta.status_code == 200, risposta.text
    assert risposta.json()["platform"] == "youtube_shorts"


@pytest.mark.parametrize(
    "valore",
    [
        "https://www.instagram.com/reel/Cxyz123/",
        "https://www.youtube.com/watch?v=abc",
        "https://example.com/creator",
        "https://instagram.com.evil.example/creator",
        "   ",
    ],
)
def test_gli_input_non_utilizzabili_sono_422(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, valore: str
) -> None:
    """Link di contenuti, host non supportati e stringhe vuote.

    `instagram.com.evil.example` e' il caso che una `in` sull'URL intero
    lascerebbe passare: l'allowlist confronta l'host per intero, come in
    `apify_service`.
    """
    risposta = _valida(client, auth_headers, valore)

    assert risposta.status_code == 422, risposta.text
    assert apify.profile_calls == []


# =============================================================================
# 2. Gli esiti: esiste / e' pubblico / non c'e' / il provider e' rotto
# =============================================================================


def test_un_profilo_pubblico_torna_con_l_anteprima(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    risposta = _valida(client, auth_headers, "instagram.com/creator")

    assert risposta.status_code == 200, risposta.text
    corpo = risposta.json()
    assert corpo["exists"] is True
    assert corpo["is_public"] is True
    assert corpo["profile"]["display_name"] == "Creator Ufficiale"
    assert corpo["profile"]["follower_count"] == 12345
    assert corpo["profile"]["is_verified"] is True
    assert corpo["profile"]["avatar_url"] == "https://cdn.example.com/avatar.jpg"


def test_un_profilo_privato_esiste_ma_non_e_pubblico(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify
) -> None:
    """`exists` e `is_public` sono due campi perche' sono due domande diverse.

    Un profilo privato ha un nome e un avatar da mostrare: la card lo mostra e
    spiega perche' non si puo' monitorare, invece di dire che non esiste.
    """
    apify.profile = ScrapedProfile(
        username="creator",
        display_name="Creator Privato",
        avatar_url="https://cdn.example.com/avatar.jpg",
        follower_count=42,
        is_private=True,
    )

    corpo = _valida(client, auth_headers, "instagram.com/creator").json()

    assert corpo["exists"] is True
    assert corpo["is_public"] is False
    assert corpo["profile"]["display_name"] == "Creator Privato"


def test_un_account_inesistente_e_un_200_con_exists_false(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify
) -> None:
    """200 e non 404: la verifica e' riuscita, ed e' il suo risultato a dire di no."""
    apify.profile = None

    risposta = _valida(client, auth_headers, "instagram.com/fantasma")

    assert risposta.status_code == 200, risposta.text
    corpo = risposta.json()
    assert corpo["exists"] is False
    assert corpo["is_public"] is False
    assert corpo["profile"] is None


def test_un_guasto_del_provider_non_diventa_account_inesistente(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    """La proprieta' piu' importante di questo endpoint.

    Se un errore di Apify fosse assorbito come `exists: false`, l'utente
    leggerebbe "questo account non esiste" ogni volta che il provider ha un
    problema — e non avrebbe modo di sapere che gli e' stato detto il falso.
    """
    apify.profile_error = ApifyError()

    risposta = _valida(client, auth_headers, "instagram.com/creator")

    assert risposta.status_code == 503, risposta.text
    assert risposta.json()["error"]["code"] == "apify_unavailable"
    assert store.rows("creator_validations") == [], (
        "un esito mai ottenuto e' finito in cache"
    )


def test_un_canale_youtube_risolvibile_e_pubblico(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube
) -> None:
    """YouTube non ha canali visibili ai soli iscritti: risolto = pubblico."""
    risposta = _valida(client, auth_headers, "https://youtube.com/@creator")

    assert risposta.status_code == 200, risposta.text
    corpo = risposta.json()
    assert corpo["exists"] is True
    assert corpo["is_public"] is True
    assert corpo["profile"]["follower_count"] == 98765, "iscritti letti come follower"
    assert youtube.calls == ["creator"]


def test_l_id_di_canale_youtube_non_viene_abbassato(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube
) -> None:
    """`UC…` e' un id, non un handle, ed e' case-sensitive.

    Tutti gli altri identificatori si abbassano — e' cio' che impedisce a
    `@Tizio` e `@tizio` di diventare due righe di cache. Applicare la stessa
    regola a un id di canale lo renderebbe irrisolvibile, e il canale
    risulterebbe inesistente per un motivo inventato da noi.
    """
    _valida(client, auth_headers, "https://www.youtube.com/channel/UCAbCdEfGhIjKlMnOpQrStUv")

    assert youtube.calls == ["UCAbCdEfGhIjKlMnOpQrStUv"]


def test_l_handle_canonico_di_youtube_vince_su_quello_scritto(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube
) -> None:
    """`customUrl` e' l'handle secondo YouTube: cache e creator devono usarlo.

    Senza, l'utente che scrive `@Creator-Ufficiale` otterrebbe una riga di cache
    su una forma e un creator su un'altra.
    """
    youtube.channel = YouTubeChannel(
        channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
        handle="CreatorReale",
        title="Creator Ufficiale",
    )

    corpo = _valida(client, auth_headers, "youtube.com/@creator-scritto-male").json()

    assert corpo["normalized_identifier"] == "creatorreale"


def test_senza_chiave_youtube_solo_youtube_si_ferma(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube
) -> None:
    """`YOUTUBE_API_KEY` e' facoltativa: la sua assenza non tocca le altre due."""
    youtube.is_configured = False

    su_youtube = _valida(client, auth_headers, "youtube.com/@creator")
    su_instagram = _valida(client, auth_headers, "instagram.com/creator")

    assert su_youtube.status_code == 503, su_youtube.text
    assert su_youtube.json()["error"]["code"] == "youtube_unavailable"
    assert su_instagram.status_code == 200, su_instagram.text


def test_il_corpo_dell_errore_non_nomina_la_variabile_mancante(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube
) -> None:
    """Il motivo resta nei log: nel corpo sarebbe una descrizione della nostra
    infrastruttura restituita a chiunque chiami l'endpoint."""
    youtube.is_configured = False

    testo = _valida(client, auth_headers, "youtube.com/@creator").text.lower()

    for frammento in ("youtube_api_key", "api key", "chiave", "googleapis"):
        assert frammento not in testo


# =============================================================================
# 2-bis. Il client YouTube, sotto la dependency
# =============================================================================
# I test qui sopra sostituiscono `YouTubeService` con un doppio, quindi non
# vedono come la chiamata viene costruita. `id` e `forHandle` sono due parametri
# diversi della stessa API e non sono intercambiabili: passare un id a
# `forHandle` non trova nulla. Si sostituisce quindi solo `_get`, cioe' il
# confine di rete, lasciando vera la logica che sceglie il parametro.


def _servizio_youtube(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, str]]:
    from app.core.config import get_settings
    from app.services.youtube_service import YouTubeService

    monkeypatch.setenv("YOUTUBE_API_KEY", "test-youtube-key")
    get_settings.cache_clear()

    catturati: dict[str, str] = {}

    async def finto_get(url: str, params: dict[str, str]) -> dict[str, Any]:
        catturati.update(params)
        # L'URL viaggia insieme ai parametri proprio perche' un test lo possa
        # osservare: e' l'unica cosa che distingueva `channels.list` da
        # `videos.list`, ed era sbagliata senza che nulla lo dicesse.
        catturati["__url__"] = url
        return {"items": []}

    servizio = YouTubeService()
    monkeypatch.setattr(servizio, "_get", finto_get)
    return servizio, catturati


async def test_un_id_di_canale_usa_il_parametro_id(monkeypatch: pytest.MonkeyPatch) -> None:
    servizio, catturati = _servizio_youtube(monkeypatch)

    await servizio.fetch_channel("UCAbCdEfGhIjKlMnOpQrStUv")

    assert catturati.get("id") == "UCAbCdEfGhIjKlMnOpQrStUv"
    assert "forHandle" not in catturati


async def test_un_handle_usa_il_parametro_forhandle(monkeypatch: pytest.MonkeyPatch) -> None:
    servizio, catturati = _servizio_youtube(monkeypatch)

    await servizio.fetch_channel("creator")

    assert catturati.get("forHandle") == "@creator"
    assert "id" not in catturati


async def test_i_metadati_di_un_video_interrogano_l_endpoint_videos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IL DIFETTO CHE NESSUN TEST VEDEVA.

    `_get` aveva l'URL di `channels.list` cablato dentro e lo usava per
    entrambe le chiamate. Non falliva: un id di video mandato a `channels.list`
    risponde 200 con `items` vuoto, perche' non e' un id di canale. Quindi
    `fetch_video` restituiva sempre `None`, il passthrough YouTube non si apriva
    mai in produzione, e ogni analisi bruciava comunque un'unita' di quota per
    una chiamata che non poteva trovare nulla.

    I test lo mancavano perche' guardavano i *parametri* e non l'URL, e perche'
    piu' in alto `FakeYouTube` sostituisce l'intero servizio con un doppio che
    un video lo restituisce.
    """
    servizio, catturati = _servizio_youtube(monkeypatch)

    await servizio.fetch_video("dQw4w9WgXcQ")

    assert catturati["__url__"].endswith("/videos"), "un id di video non va a channels.list"
    assert catturati.get("id") == "dQw4w9WgXcQ"
    assert "contentDetails" in catturati.get("part", ""), "senza durata il passthrough non parte"


async def test_la_verifica_di_un_canale_interroga_l_endpoint_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servizio, catturati = _servizio_youtube(monkeypatch)

    await servizio.fetch_channel("creator")

    assert catturati["__url__"].endswith("/channels")


async def test_un_id_di_canale_digitato_a_mano_conserva_le_maiuscole() -> None:
    """L'id di canale e' l'unico identificatore YouTube case-sensitive.

    Dal link `youtube.com/channel/UC…` era gia' salvo, perche' li' il prefisso
    `/channel/` lo annuncia. Digitato nudo non ha prefisso, veniva abbassato
    come un handle qualsiasi, non superava piu' `is_channel_id` e finiva su
    `forHandle`: un canale esistente veniva dichiarato inesistente.
    """
    from app.services.creator_validation import parse_creator_input

    nudo = parse_creator_input("UCAbCdEfGhIjKlMnOpQrStUv", "youtube_shorts")
    da_url = parse_creator_input("https://youtube.com/channel/UCAbCdEfGhIjKlMnOpQrStUv", None)
    handle = parse_creator_input("@Creator", "youtube_shorts")

    assert nudo.identifier == "UCAbCdEfGhIjKlMnOpQrStUv"
    assert da_url.identifier == "UCAbCdEfGhIjKlMnOpQrStUv"
    assert handle.identifier == "creator", "un handle resta case-insensitive"


async def test_la_chiave_non_finisce_mai_in_un_parametro_diverso_da_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La credenziale viaggia solo dove deve, e non si moltiplica per errore."""
    servizio, catturati = _servizio_youtube(monkeypatch)

    await servizio.fetch_channel("creator")

    assert catturati["key"] == "test-youtube-key"
    assert [nome for nome, valore in catturati.items() if valore == "test-youtube-key"] == ["key"]


# =============================================================================
# 3. La cache
# =============================================================================


def test_la_seconda_verifica_non_richiama_il_provider(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    prima = _valida(client, auth_headers, "instagram.com/creator")
    seconda = _valida(client, auth_headers, "instagram.com/creator")

    assert prima.status_code == 200 and seconda.status_code == 200
    assert len(apify.profile_calls) == 1, "il provider e' stato richiamato: cache inefficace"
    assert len(store.rows("creator_validations")) == 1
    # `checked_at` resta quello della verifica **vera**: e' cosi' che il client
    # sa quanto e' vecchio il dato che sta guardando.
    assert prima.json()["checked_at"] == seconda.json()["checked_at"]


def test_forme_diverse_dello_stesso_handle_condividono_la_riga(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    """`@Creator`, `creator` e l'URL sono lo stesso profilo, quindi un solo costo."""
    _valida(client, auth_headers, "@Creator", "instagram")
    _valida(client, auth_headers, "creator", "instagram")
    _valida(client, auth_headers, "https://www.instagram.com/CREATOR/")

    assert len(apify.profile_calls) == 1
    assert len(store.rows("creator_validations")) == 1


def test_la_cache_di_un_utente_serve_anche_gli_altri(
    client: TestClient,
    auth_headers: dict[str, str],
    other_auth_headers: dict[str, str],
    apify: FakeApify,
) -> None:
    """La cache e' condivisa di proposito: il profilo di un creator non e' un
    dato di nessuno, e una cache per utente ripagherebbe il provider una volta
    per utente interessato allo stesso creator."""
    _valida(client, auth_headers, "instagram.com/creator")
    risposta = _valida(client, other_auth_headers, "instagram.com/creator")

    assert risposta.status_code == 200, risposta.text
    assert len(apify.profile_calls) == 1


def test_oltre_il_ttl_si_rivalida(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    """Un profilo passato a privato non deve restare pubblico per sempre."""
    _valida(client, auth_headers, "instagram.com/creator")

    vecchio = datetime.now(UTC) - timedelta(hours=25)
    store.rows("creator_validations")[0]["checked_at"] = vecchio.isoformat()
    apify.profile = ScrapedProfile(username="creator", is_private=True)

    corpo = _valida(client, auth_headers, "instagram.com/creator").json()

    assert len(apify.profile_calls) == 2, "la riga scaduta e' stata servita comunque"
    assert corpo["is_public"] is False
    assert len(store.rows("creator_validations")) == 1, "la riga scaduta va sovrascritta"


def test_una_riga_di_cache_illeggibile_non_rompe_l_endpoint(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    """Una riga scritta da una versione precedente dello schema si rivalida.

    L'alternativa — sollevare — trasformerebbe l'evoluzione dello schema in un
    errore per ogni utente che tocca quel creator, e la riga verrebbe comunque
    sovrascritta.
    """
    _valida(client, auth_headers, "instagram.com/creator")
    store.rows("creator_validations")[0]["response"] = {"forma": "di un altro schema"}

    risposta = _valida(client, auth_headers, "instagram.com/creator")

    assert risposta.status_code == 200, risposta.text
    assert risposta.json()["exists"] is True
    assert len(apify.profile_calls) == 2


# =============================================================================
# 4. La quota: cio' che la cache da sola non copre
# =============================================================================


def test_una_verifica_pagata_consuma_quota(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    _valida(client, auth_headers, "instagram.com/creator")

    eventi = store.rows("validation_events")
    assert len(eventi) == 1
    assert eventi[0]["user_id"] == USER_ID
    assert eventi[0]["platform"] == "instagram"
    assert eventi[0]["normalized_identifier"] == "creator"


def test_un_cache_hit_non_consuma_quota(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """Non costa nulla, quindi non deve costare quota: e' la ragione per cui il
    consumo sta dopo il controllo di cache e non all'ingresso dell'endpoint."""
    _valida(client, auth_headers, "instagram.com/creator")
    _valida(client, auth_headers, "instagram.com/creator")

    assert len(store.rows("validation_events")) == 1


def test_identificatori_diversi_consumano_ognuno_il_suo(
    client: TestClient, auth_headers: dict[str, str], store: FakeStore
) -> None:
    """IL VETTORE CHE LA CACHE NON CHIUDE.

    Validare 5.000 handle diversi produce 5.000 cache miss, cioe' 5.000
    chiamate esterne pagate. Se questi eventi non venissero contati, la cache
    darebbe l'illusione di una protezione che non c'e'.
    """
    for indice in range(5):
        _valida(client, auth_headers, f"instagram.com/creator{indice}")

    assert len(store.rows("validation_events")) == 5


def test_un_provider_che_fallisce_consuma_comunque_la_quota(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify, store: FakeStore
) -> None:
    """L'actor e' partito ed e' stato fatturato: la spesa c'e' stata lo stesso.

    E' lo stesso principio di `analysis_events`, e lo stesso caso che contare
    solo gli esiti riusciti avrebbe perso.
    """
    apify.profile_error = ApifyError()

    assert _valida(client, auth_headers, "instagram.com/creator").status_code == 503
    assert len(store.rows("validation_events")) == 1


def test_lo_sqlstate_px003_diventa_un_errore_di_quota() -> None:
    errore = translate_postgrest_error(
        FakeAPIError("PX003", MESSAGGIO_POSTGRES), context="quota validazioni"
    )

    assert isinstance(errore, ValidationQuotaError)
    assert errore.status_code == 409
    assert errore.code == "validation_quota_reached"


def test_la_quota_esaurita_arriva_al_client_come_409_pulito(
    client: TestClient,
    auth_headers: dict[str, str],
    apify: FakeApify,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Percorso completo: rifiuto del database -> PostgREST -> envelope HTTP.

    Il rifiuto va iniettato **dentro l'insert**, dove il trigger lo
    solleverebbe, e non al posto di `_consuma_quota`: sostituire la funzione
    scavalcherebbe il suo `db_errors`, cioe' proprio la traduzione da
    verificare.
    """
    originale = store.enforce_unique

    def rifiuta_validation_events(table: str, values: Any) -> None:
        if table == "validation_events":
            raise FakeAPIError("PX003", MESSAGGIO_POSTGRES)
        originale(table, values)

    monkeypatch.setattr(store, "enforce_unique", rifiuta_validation_events)

    risposta = _valida(client, auth_headers, "instagram.com/creator")

    assert risposta.status_code == 409, risposta.text
    assert risposta.json()["error"]["code"] == "validation_quota_reached"
    assert "PX003" not in risposta.text
    assert apify.profile_calls == [], "provider chiamato nonostante la quota esaurita"


# =============================================================================
# 5. Perimetro
# =============================================================================


def test_senza_jwt_non_si_valida_nulla(client: TestClient, apify: FakeApify) -> None:
    """Un endpoint che spende denaro non ha una versione aperta."""
    risposta = client.post("/api/v1/creators/validate", json={"input": "instagram.com/x"})

    assert risposta.status_code == 401
    assert apify.profile_calls == []


def test_la_risposta_non_espone_campi_interni(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify
) -> None:
    """Il modello di risposta e' un filtro d'uscita: i payload dei provider
    contengono molto altro — email di contatto, categoria business, id interni —
    e nulla di tutto cio' e' dichiarato in `CreatorProfilePreview`."""
    corpo = _valida(client, auth_headers, "instagram.com/creator").json()

    assert set(corpo) == {
        "platform",
        "normalized_identifier",
        "exists",
        "is_public",
        "profile",
        "checked_at",
    }
    assert set(corpo["profile"]) == {
        "avatar_url",
        "display_name",
        "username",
        "is_verified",
        "follower_count",
    }


def test_un_avatar_con_schema_inatteso_viene_scartato(
    client: TestClient, auth_headers: dict[str, str], apify: FakeApify
) -> None:
    """L'avatar arriva da un provider esterno e finisce in un `src`: si scarta
    all'ingresso, dove il valore entra nel sistema, non nel frontend."""
    apify.profile = ScrapedProfile(
        username="creator", avatar_url="javascript:alert(1)", follower_count=1
    )

    corpo = _valida(client, auth_headers, "instagram.com/creator").json()

    assert corpo["profile"]["avatar_url"] is None
