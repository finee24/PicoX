"""`provider_parsing` non solleva mai: è il suo unico contratto forte.

Legge JSON che arriva da Apify e dalla YouTube Data API — **input non fidato**,
nel senso che nessuno ci garantisce la forma. Un parser che esplode su un campo
accessorio farebbe fallire un'analisi già pagata per un numero che finisce in una
card, quindi la regola è: davanti a un valore illeggibile si restituisce `None`,
mai un'eccezione e mai un default inventato.

Il file esiste perché unificare cinque copie ha rischiato di romperlo. La
revisione di sicurezza della PR ha trovato che la versione unificata sollevava
`OverflowError` su `Infinity` dove quella di YouTube restituiva `None`: una
regressione silenziosa in un refactoring dichiarato a comportamento invariato.
Correggendola sono emersi **due difetti preesistenti a `main`**, che ora sono
chiusi e qui bloccati:

* `json.loads` della stdlib accetta `Infinity` e `NaN`, che JSON non prevede, e
  `int(inf)` solleva `OverflowError` — capitava sul percorso Apify;
* `"²".isdigit()` è `True` ma `int("²")` solleva `ValueError` — capitava su
  entrambi i percorsi.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.provider_parsing import come_datetime, contatore, primo_contatore

# Valori che un provider non dovrebbe mandare e che prima o poi manda.
VALORI_OSTILI = [
    float("inf"),
    float("-inf"),
    float("nan"),
    "²",  # isdigit() è True, int() solleva
    "12e400",
    "",
    "   ",
    "-5",
    None,
    True,  # bool è sottotipo di int
    False,
    [],
    {},
    object(),
]


@pytest.mark.parametrize("valore", VALORI_OSTILI)
def test_un_contatore_illeggibile_da_none_e_non_solleva(valore: object) -> None:
    assert contatore(valore) is None


@pytest.mark.parametrize("valore", VALORI_OSTILI)
def test_anche_sondando_piu_chiavi_non_si_solleva(valore: object) -> None:
    assert primo_contatore({"likesCount": valore, "likes": valore}, ("likesCount", "likes")) is None


@pytest.mark.parametrize(
    ("grezzo", "atteso"),
    [
        (12300, 12300),
        ("12300", 12300),  # la Data API manda sempre stringhe
        ("1.234", 1234),  # alcuni actor formattano le migliaia
        ("1,234", 1234),
        (12300.0, 12300),
        (0, 0),  # zero è un conteggio vero, non un valore mancante
    ],
)
def test_i_conteggi_leggibili_passano(grezzo: object, atteso: int) -> None:
    assert contatore(grezzo) == atteso


def test_una_chiave_illeggibile_non_ferma_la_ricerca_sulle_altre() -> None:
    """«C'è ma non si capisce» e «non c'è» portano alla stessa domanda."""
    item = {"likesCount": float("inf"), "diggCount": "²", "likes": "42"}

    assert primo_contatore(item, ("likesCount", "diggCount", "likes")) == 42


@pytest.mark.parametrize("valore", ["", "non una data", "2026-13-45T00:00:00Z", 42, None, []])
def test_una_data_illeggibile_da_none_e_non_solleva(valore: object) -> None:
    assert come_datetime(valore) is None


def test_una_data_senza_fuso_viene_assunta_utc() -> None:
    """Un naive confrontato con un aware solleva TypeError: qui si chiude prima."""
    istante = come_datetime("2026-08-16T10:30:00")

    assert istante is not None
    assert istante.tzinfo is not None
    assert istante == datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


def test_la_z_di_rfc3339_viene_riconosciuta() -> None:
    """È la forma che manda la Data API, e `fromisoformat` non la accetta ovunque."""
    assert come_datetime("2026-08-16T10:30:00Z") == datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


# =============================================================================
# L'autore: identificatore o nome scelto da chi pubblica?
# =============================================================================
#
# `analysis_service._PIATTAFORME_CON_HANDLE_AFFIDABILE` ammette `tiktok` e
# `instagram` nella deduzione automatica del creator, e l'ammissione poggia su
# una proprieta' che **non vive in quel file**: che per quelle due piattaforme
# `author_username` esca da una chiave-handle e non da una chiave-nome.
#
# `_AUTHOR_KEYS` e' una tupla sola, condivisa fra tutte le piattaforme, e
# contiene anche `channelName`/`channelTitle`/`author`, che sono nomi
# visualizzati. `_first_str` prende la prima che risponde: **l'ordine e' la
# garanzia**. Un riordino, o una chiave nuova messa in cima, sposterebbe quella
# garanzia senza che nulla in `analysis_service` cambi — e l'allowlist
# ammetterebbe una piattaforma su cui l'attribuzione e' influenzabile da chi
# pubblica il video. Questi test legano le due cose.


def test_su_instagram_l_autore_esce_dalla_chiave_handle() -> None:
    """`ownerUsername` e' l'handle; `fullName` e' il nome scelto dall'utente."""
    from app.services.apify_service import _normalize_item

    video = _normalize_item(
        {
            "url": "https://www.instagram.com/reel/Cxyz12345/",
            "videoUrl": "https://cdn.example.com/v.mp4",
            "ownerUsername": "ingegneri_in_borsa",
            # Il nome visualizzato, che non deve mai vincere sull'handle.
            "ownerFullName": "Ingegneri in Borsa",
            "author": "Ingegneri in Borsa",
        }
    )

    assert video is not None
    assert video.author_username == "ingegneri_in_borsa", (
        "l'autore e' stato letto da una chiave di nome visualizzato: "
        "l'allowlist di _creator_seguito non regge piu' su instagram"
    )


def test_su_tiktok_l_autore_esce_da_author_meta_e_non_dal_nickname() -> None:
    """Su TikTok l'handle sta in `authorMeta.uniqueId`.

    `nickName` e' nello stesso oggetto ed e' il nome scelto dall'utente,
    modificabile a piacere: sta in `_DISPLAY_NAME_KEYS`, che `_normalize_item`
    non consulta per l'autore. Se ci finisse, un creator potrebbe farsi passare
    per un altro cambiando il proprio nickname.
    """
    from app.services.apify_service import _normalize_item

    video = _normalize_item(
        {
            "webVideoUrl": "https://www.tiktok.com/@creator/video/123",
            "videoUrl": "https://cdn.example.com/v.mp4",
            "authorMeta": {
                "uniqueId": "geopop",
                "nickName": "Qualcun Altro",
                "fullName": "Qualcun Altro",
            },
        }
    )

    assert video is not None
    assert video.author_username == "geopop", (
        "l'autore e' stato letto dal nickname: su tiktok l'attribuzione "
        "diventerebbe influenzabile da chi pubblica"
    )


def test_su_youtube_l_autore_e_un_nome_ed_e_il_motivo_dell_esclusione() -> None:
    """Il complemento: qui la chiave che risponde **e'** un nome visualizzato.

    Non e' un difetto di `_normalize_item` — non esiste di meglio nell'item
    YouTube — ed e' esattamente la ragione per cui `youtube_shorts` resta fuori
    dall'allowlist. Se un giorno questo test iniziasse a fallire perche' arriva
    un handle vero, sarebbe il segnale che l'esclusione si puo' rivedere.
    """
    from app.services.apify_service import _normalize_item

    video = _normalize_item(
        {
            "url": "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "videoUrl": "https://cdn.example.com/v.mp4",
            "channelName": "Geopop",
        }
    )

    assert video is not None
    assert video.author_username == "Geopop"
