"""`detect_platform` deve guardare l'host, non una sottostringa dell'URL.

Il controllo era `if "instagram." in url`, valutato sull'URL intero e in
minuscolo. Bastava quindi un path per farsi classificare come una piattaforma
supportata — `https://evil.example/instagram.mp4` — e l'URL di un terzo finiva
spedito all'actor Apify di Instagram.

Non è un buco di autenticazione: l'analisi resta comunque protetta dai controlli
di `media_service`. È però una chiamata Apify pagata su un input che non c'entra
nulla, e un URL arbitrario consegnato a un servizio esterno.
"""

from __future__ import annotations

import pytest

from app.services.apify_service import detect_platform


@pytest.mark.parametrize(
    ("url", "atteso"),
    [
        ("https://www.instagram.com/reel/ABC123/", "instagram"),
        ("https://instagram.com/p/ABC123/", "instagram"),
        ("https://m.instagram.com/reel/ABC123/", "instagram"),
        ("https://www.tiktok.com/@tizio/video/123", "tiktok"),
        ("https://vm.tiktok.com/ZMabc/", "tiktok"),
        ("https://vt.tiktok.com/ZTabc/", "tiktok"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube_shorts"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube_shorts"),
        ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "youtube_shorts"),
        # Maiuscole nell'host: il DNS non fa distinzione, l'allowlist nemmeno.
        ("https://WWW.TikTok.com/@tizio/video/123", "tiktok"),
    ],
)
def test_le_piattaforme_vere_restano_riconosciute(url: str, atteso: str) -> None:
    assert detect_platform(url) == atteso


@pytest.mark.parametrize(
    "url",
    [
        # Il caso del report: la piattaforma compare nel *path*.
        "https://evil.example/instagram.mp4",
        "https://evil.example/tiktok.mp4",
        "https://evil.example/youtube.com/video.mp4",
        # Sottodominio ostile che contiene il dominio vero.
        "https://instagram.com.evil.example/reel/ABC",
        "https://tiktok.com.evil.example/video/1",
        # Suffisso ostile: `in` avrebbe accettato anche questo.
        "https://notinstagram.com/reel/ABC",
        "https://eviltiktok.com/@tizio/video/1",
        # Host plausibile ma non nella allowlist.
        "https://cdn.tiktokcdn.com/video.mp4",
        # Senza host non c'è nulla da classificare.
        "non-un-url",
        "",
    ],
)
def test_gli_host_non_in_allowlist_non_vengono_classificati(url: str) -> None:
    assert detect_platform(url) is None


def test_il_punto_finale_di_un_fqdn_non_aggira_l_allowlist() -> None:
    """`instagram.com.` risolve in DNS come `instagram.com`.

    Se l'allowlist non normalizzasse il punto finale, questo URL sfuggirebbe al
    riconoscimento pur puntando alla piattaforma vera.
    """
    assert detect_platform("https://instagram.com./reel/ABC123/") == "instagram"
