"""Canonicalizzazione degli URL (voce A9), e la copertura differita di A10.

Ogni chiave diversa per lo stesso video e' **un'analisi pagata due volte**:
`UNIQUE (user_id, video_url)` deduplica cio' che riceve, non cio' che significa.
La review avversariale aveva misurato 9 URL dello stesso video -> 9 righe -> 9
inferenze.

GRUPPO DI CONTROLLO. Ogni equivalenza nuova e' verificata due volte: che la
funzione attuale la riconosca, e che quella **precedente** non la riconoscesse.
`_vecchia_normalize` qui sotto riproduce la logica di prima riga per riga. Senza,
un test verde non direbbe se ha corretto qualcosa o se il caso funzionava gia'.

IL VINCOLO CHE LIMITA TUTTO. Il valore prodotto finisce in `insights.video_url`,
che `insight-card.tsx` rende come `href` cliccabile: **la forma canonica deve
restare un URL che apre il video**. E' il motivo per cui lo username TikTok resta
nella chiave anche se toglierlo darebbe una chiave piu' robusta.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from app.core.exceptions import PicoxValidationError
from app.services.media_service import _is_tracking_param, normalize_video_url

# Gli alias di host della versione precedente, riprodotti tali e quali.
_VECCHI_ALIAS = {
    "www.instagram.com": "instagram.com",
    "m.instagram.com": "instagram.com",
    "www.tiktok.com": "tiktok.com",
    "m.tiktok.com": "tiktok.com",
    "vm.tiktok.com": "tiktok.com",
    "www.youtube.com": "youtube.com",
    "m.youtube.com": "youtube.com",
    "youtu.be": "youtube.com",
}


def _vecchia_normalize(raw_url: str) -> str:
    """La funzione com'era prima di A9. Serve solo come gruppo di controllo."""
    parts = urlsplit(raw_url.strip())
    host = parts.hostname or ""
    host = host.removeprefix("www.") if host not in _VECCHI_ALIAS else host
    host = _VECCHI_ALIAS.get(host, host)
    path = parts.path.rstrip("/") or "/"
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not _is_tracking_param(k)
    )
    return urlunsplit(("https", host, path, urlencode(kept), ""))


CANONICO_IG = "https://instagram.com/reel/ABC123"
CANONICO_TT = "https://tiktok.com/@tizio/video/7522889737288305942"


# =============================================================================
# 1. Le forme di path di una piattaforma collassano su una chiave sola
# =============================================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/reel/ABC123",
        "https://instagram.com/reel/ABC123/",
        "https://www.instagram.com/reel/ABC123/",
        "https://m.instagram.com/reel/ABC123/",
        "https://instagram.com/reels/ABC123/",
        "https://instagram.com/p/ABC123/",
        "https://instagram.com/tv/ABC123/",
        "https://INSTAGRAM.com/reel/ABC123/",
        "https://instagram.com./reel/ABC123/",
        "https://instagram.com/reel/ABC123/#commenti",
        "https://instagram.com/reel/ABC123/?igsh=abc&utm_source=x",
        "http://instagram.com/reel/ABC123/",
        "https://instagram.com:443/reel/ABC123/",
        "https://instagram.com/reel/%41BC123/",
    ],
)
def test_ogni_forma_instagram_da_la_stessa_chiave(url: str) -> None:
    assert normalize_video_url(url) == CANONICO_IG


@pytest.mark.parametrize(
    "url",
    [
        "https://tiktok.com/@tizio/video/7522889737288305942",
        "https://tiktok.com/@tizio/video/7522889737288305942/",
        "https://www.tiktok.com/@tizio/video/7522889737288305942",
        "https://m.tiktok.com/@tizio/video/7522889737288305942/",
        "https://www.tiktok.com/@tizio/video/7522889737288305942?is_from_webapp=1&sender_device=pc",
        "https://tiktok.com./@tizio/video/7522889737288305942",
        "https://TikTok.com/@tizio/video/7522889737288305942",
    ],
)
def test_ogni_forma_tiktok_da_la_stessa_chiave(url: str) -> None:
    assert normalize_video_url(url) == CANONICO_TT


# =============================================================================
# 2. Gruppo di controllo: la vecchia funzione su questi casi falliva
# =============================================================================


@pytest.mark.parametrize(
    ("a", "b", "motivo"),
    [
        ("https://instagram.com/p/ABC123/", "https://instagram.com/reel/ABC123/",
         "forme di path diverse dello stesso post"),
        ("https://instagram.com/reels/ABC123/", "https://instagram.com/reel/ABC123/",
         "alias plurale di Instagram"),
        ("https://instagram.com/tv/ABC123/", "https://instagram.com/reel/ABC123/",
         "ex IGTV"),
        ("https://instagram.com./reel/ABC123/", "https://instagram.com/reel/ABC123/",
         "punto finale dell'FQDN"),
        ("https://instagram.com/reel/%41BC123/", "https://instagram.com/reel/ABC123/",
         "percent-encoding di un carattere non riservato"),
    ],
)
def test_la_vecchia_funzione_su_questi_casi_dava_due_chiavi(
    a: str, b: str, motivo: str
) -> None:
    """Prova che il fix ha corretto qualcosa e non stia certificando lo status quo."""
    assert _vecchia_normalize(a) != _vecchia_normalize(b), (
        f"il caso «{motivo}» funzionava gia' prima: il test non dimostra nulla"
    )
    assert normalize_video_url(a) == normalize_video_url(b)


# =============================================================================
# 3. Cio' che NON deve collassare
# =============================================================================


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Video diversi della stessa piattaforma.
        ("https://instagram.com/reel/ABC123/", "https://instagram.com/reel/XYZ789/"),
        ("https://tiktok.com/@tizio/video/111", "https://tiktok.com/@tizio/video/222"),
        # Stesso codice su piattaforme diverse.
        ("https://instagram.com/reel/ABC123/", "https://tiktok.com/@x/video/123"),
        # Un parametro che NON e' di tracciamento cambia la risorsa.
        ("https://esempio.example/v?id=1", "https://esempio.example/v?id=2"),
        # Un link breve non e' riscrivibile senza rete: resta una chiave a se'.
        ("https://vm.tiktok.com/ZMabc/", "https://tiktok.com/@tizio/video/111"),
    ],
)
def test_risorse_distinte_restano_distinte(a: str, b: str) -> None:
    """Il rischio speculare della canonicalizzazione: **fondere video diversi**,
    e restituire a un utente l'analisi di un altro video."""
    assert normalize_video_url(a) != normalize_video_url(b)


def test_un_link_breve_non_viene_riscritto_in_un_url_inesistente() -> None:
    """Il difetto che A9 ha corretto, e che era visibile all'utente.

    La vecchia funzione riscriveva l'host di `vm.tiktok.com` in `tiktok.com`
    lasciando il path del link breve: il risultato era `https://tiktok.com/ZMabc`,
    **un indirizzo che non esiste** — e quel valore finisce in
    `insights.video_url`, che il frontend rende come link cliccabile.
    """
    vecchio = _vecchia_normalize("https://vm.tiktok.com/ZMabc/")
    nuovo = normalize_video_url("https://vm.tiktok.com/ZMabc/")

    assert vecchio == "https://tiktok.com/ZMabc", "il controllo non riproduce il difetto"
    assert nuovo == "https://vm.tiktok.com/ZMabc"


# =============================================================================
# 4. Il ramo generico: cio' che nessuna regola prevede deve continuare a funzionare
# =============================================================================


@pytest.mark.parametrize(
    ("url", "atteso"),
    [
        # Path di una piattaforma nota che non combacia con alcuna forma
        # canonica: si applica la sola normalizzazione generica.
        ("https://www.instagram.com/stories/tizio/123/", "https://instagram.com/stories/tizio/123"),
        # Link diretto a un media, il caso in cui `detect_platform` torna None.
        ("https://cdn.example.com/video.mp4?utm_source=x", "https://cdn.example.com/video.mp4"),
        # `www.` tolto anche su host non noti.
        ("https://www.esempio.example/v/1/", "https://esempio.example/v/1"),
        # Parametri riordinati.
        ("https://esempio.example/v?b=2&a=1", "https://esempio.example/v?a=1&b=2"),
        # Frammento eliminato.
        ("https://esempio.example/v#t=30", "https://esempio.example/v"),
    ],
)
def test_il_ramo_generico_resta_quello_di_prima(url: str, atteso: str) -> None:
    assert normalize_video_url(url) == atteso


# =============================================================================
# 5. La forma canonica deve restare un URL apribile
# =============================================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/p/ABC123/",
        "https://www.tiktok.com/@tizio/video/7522889737288305942?is_from_webapp=1",
        "https://vm.tiktok.com/ZMabc/",
        "https://cdn.example.com/video.mp4",
    ],
)
def test_la_chiave_e_sempre_un_url_http_navigabile(url: str) -> None:
    """`insights.video_url` viene reso come `href` da `insight-card.tsx`, dove
    `httpUrlOrNull` accetta solo http/https. Una chiave che non passa quel filtro
    romperebbe il link per ogni insight."""
    chiave = normalize_video_url(url)
    parti = urlsplit(chiave)

    assert parti.scheme == "https"
    assert parti.hostname, "senza host il link non si apre"
    assert not parti.fragment
    assert " " not in chiave


# =============================================================================
# 6. Validazione dell'input (copertura differita da A10)
# =============================================================================


@pytest.mark.parametrize(
    "url",
    ["", "   ", "non-un-url", "https://", "https:///percorso",
     "file:///etc/passwd", "javascript:alert(1)", "ftp://esempio.example/v.mp4"],
)
def test_gli_input_non_validi_vengono_rifiutati(url: str) -> None:
    with pytest.raises(PicoxValidationError):
        normalize_video_url(url)


def test_le_credenziali_non_finiscono_nella_chiave() -> None:
    """Una credenziale nella chiave sarebbe anche un segreto scritto in chiaro su
    una riga di `insights`."""
    chiave = normalize_video_url("https://utente:parola@instagram.com/reel/ABC123/")

    assert chiave == CANONICO_IG
    assert "utente" not in chiave and "parola" not in chiave


def test_la_normalizzazione_e_idempotente() -> None:
    """Rinormalizzare una chiave gia' canonica non deve cambiarla.

    Se non lo fosse, un valore riletto dal database e ripassato dalla funzione
    produrrebbe una chiave diversa da quella con cui e' stato salvato.
    """
    for url in (
        "https://instagram.com/p/ABC123/",
        "https://www.tiktok.com/@tizio/video/7522889737288305942/",
        "https://www.esempio.example/v/1/?utm_source=x",
        "https://vm.tiktok.com/ZMabc/",
    ):
        una = normalize_video_url(url)
        assert normalize_video_url(una) == una, f"non idempotente su {url}"
