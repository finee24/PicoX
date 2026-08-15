"""Scraper per piattaforma, passthrough YouTube e leve sui token.

LE PROPRIETA' CHE CONTANO DI PIU'.

1. **Il passthrough non deve aprire un buco sui limiti.** Non scaricando il
   file, `download_to_temp` non gira — ed e' lui ad applicare
   `MAX_VIDEO_DURATION_SECONDS`. Se la durata non e' verificabile prima, il
   passthrough non si fa: si torna al percorso che il limite lo applica.
2. **Il bivio a valle e' uno solo.** `run_analysis` guarda `youtube_url` e
   nient'altro: nessun `if platform ==` da nessuna parte dopo lo scraper.
3. **Le due leve sui token sono legate alla modalita' scelta dall'utente**, non
   a una costante globale, e valgono su entrambi i percorsi.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.scraping import ScraperResult
from app.services.apify_service import ScrapedVideo, _normalize_item
from app.services.content_scraper import da_video_scrapato
from app.services.gemini_service import build_video_part, preset_per_modalita
from app.services.youtube_service import video_id_from_url
from tests.conftest import USER_ID, FakeApify, FakeGemini, FakeYouTube
from tests.fake_supabase import FakeStore

YOUTUBE_URL = "https://www.youtube.com/shorts/dQw4w9WgXcQ"
INSTAGRAM_URL = "https://www.instagram.com/reel/Cxyz12345/"


def _analizza(client: TestClient, headers: dict[str, str], url: str, mode: str = "BOTH"):
    return client.post(
        "/api/v1/analyze-video",
        headers=headers,
        json={"video_url": url, "analysis_mode": mode},
    )


# =============================================================================
# 1. YouTube: l'URL va a Gemini, i byte restano dove sono
# =============================================================================


def test_un_video_youtube_non_viene_scaricato(
    client: TestClient,
    auth_headers: dict[str, str],
    gemini: FakeGemini,
    apify: FakeApify,
    downloads: list[str],
) -> None:
    """Il guadagno del passthrough e' proprio questo: zero byte da noi."""
    risposta = _analizza(client, auth_headers, YOUTUBE_URL)

    assert risposta.status_code == 201, risposta.text
    assert len(gemini.url_calls) == 1, "il video non e' stato passato per URL"
    assert gemini.url_calls[0][0] == "https://youtube.com/shorts/dQw4w9WgXcQ"
    assert gemini.calls == [], "e' stato usato il percorso con upload"
    assert downloads == [], "il video e' stato scaricato nonostante il passthrough"
    assert apify.resolve_calls == [], "Apify chiamato per un video che non lo richiede"


def test_i_metadati_youtube_arrivano_dalla_data_api(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube,
    store: FakeStore,
) -> None:
    """`videos.list` copre copertina e durata senza pagare uno scraper."""
    risposta = _analizza(client, auth_headers, YOUTUBE_URL)

    assert risposta.status_code == 201, risposta.text
    assert youtube.video_calls == ["dQw4w9WgXcQ"]
    assert risposta.json()["thumbnail_url"] == "https://i.ytimg.example/thumb.jpg"
    assert store.rows("insights")[0]["thumbnail_url"] == "https://i.ytimg.example/thumb.jpg"


def test_un_video_troppo_lungo_e_rifiutato_prima_di_pagare(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube,
    gemini: FakeGemini, store: FakeStore,
) -> None:
    """IL CASO CHE IL PASSTHROUGH POTEVA APRIRE.

    Senza download non c'e' nulla da interrompere a meta': se il limite non
    scattasse qui, `MAX_VIDEO_DURATION_SECONDS` semplicemente non varrebbe su
    YouTube — e sarebbe l'inferenza a pagarne il conto, non una CDN.
    """
    assert youtube.video is not None
    youtube.video.duration_seconds = get_settings().max_video_duration_seconds + 60

    risposta = _analizza(client, auth_headers, YOUTUBE_URL)

    assert risposta.status_code == 422, risposta.text
    assert risposta.json()["error"]["code"] == "video_too_long"
    assert gemini.url_calls == [] and gemini.calls == []
    assert store.rows("insights") == []


@pytest.mark.parametrize("motivo", ["chiave assente", "durata sconosciuta"])
def test_senza_durata_verificabile_si_torna_al_download(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube,
    gemini: FakeGemini, apify: FakeApify, downloads: list[str], motivo: str,
) -> None:
    """Il ripiego non e' un dettaglio: e' cio' che rende sicuro il passthrough.

    Senza metadati non sapremmo quanto dura il video, e il percorso con download
    e' l'unico che applica i limiti misurandoli davvero.
    """
    if motivo == "chiave assente":
        youtube.is_configured = False
    else:
        assert youtube.video is not None
        youtube.video.duration_seconds = None

    risposta = _analizza(client, auth_headers, YOUTUBE_URL)

    assert risposta.status_code == 201, risposta.text
    assert gemini.url_calls == [], "passthrough eseguito senza poter verificare la durata"
    assert len(gemini.calls) == 1, "il percorso con upload non e' stato usato"
    assert apify.resolve_calls == [YOUTUBE_URL.replace("www.", "")]
    assert downloads == ["https://cdn.example.com/video-123.mp4"]


def test_un_id_video_non_estraibile_non_blocca_l_analisi(
    client: TestClient, auth_headers: dict[str, str], youtube: FakeYouTube,
    gemini: FakeGemini,
) -> None:
    """Una forma di URL che non conosciamo ricade sul percorso di sempre."""
    risposta = _analizza(client, auth_headers, "https://www.youtube.com/@creator/live")

    assert risposta.status_code == 201, risposta.text
    assert youtube.video_calls == [], "chiamata la Data API senza un id da cercare"
    assert len(gemini.calls) == 1


@pytest.mark.parametrize(
    ("url", "atteso"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=30", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        # Un id ha 11 caratteri: una stringa piu' corta non e' un id, e mandarla
        # alla Data API spenderebbe una unita' di quota per un "non trovato".
        ("https://www.youtube.com/shorts/corto", None),
        ("https://www.youtube.com/@creator", None),
        ("https://vimeo.com/12345678901", None),
    ],
)
def test_estrazione_dell_id_video(url: str, atteso: str | None) -> None:
    assert video_id_from_url(url) == atteso


# =============================================================================
# 2. Instagram e TikTok: byte, come prima
# =============================================================================


def test_instagram_passa_da_apify_e_dalla_files_api(
    client: TestClient, auth_headers: dict[str, str], gemini: FakeGemini,
    apify: FakeApify, downloads: list[str],
) -> None:
    """Nessun passthrough nativo: l'unico modo e' mandare i byte."""
    risposta = _analizza(client, auth_headers, INSTAGRAM_URL)

    assert risposta.status_code == 201, risposta.text
    assert gemini.url_calls == [], "un URL Instagram non e' risolvibile da Gemini"
    assert len(gemini.calls) == 1
    assert apify.resolve_calls == ["https://instagram.com/reel/Cxyz12345"]
    assert downloads == ["https://cdn.example.com/video-123.mp4"]


def test_i_contatori_del_post_arrivano_dallo_stesso_item(store: FakeStore) -> None:
    """Like, view, commenti e autore stanno nell'item che porta gia' l'URL.

    Leggerli non costa una chiamata in piu': costerebbe non leggerli, perche'
    servirebbe un secondo giro per gli stessi dati.
    """
    video = _normalize_item(
        {
            "url": "https://www.instagram.com/reel/Cxyz12345/",
            "videoUrl": "https://cdn.example.com/v.mp4",
            "caption": "Come studiare la sera",
            "likesCount": 1200,
            "videoPlayCount": 45000,
            "commentsCount": 87,
            "ownerUsername": "creator",
            "timestamp": "2026-08-01T10:00:00Z",
        }
    )

    assert video is not None
    assert video.like_count == 1200
    assert video.view_count == 45000
    assert video.comment_count == 87
    assert video.author_username == "creator"


def test_i_contatori_tiktok_arrivano_da_authormeta() -> None:
    """Stesso dato, nomi diversi e un livello di annidamento in piu'."""
    video = _normalize_item(
        {
            "webVideoUrl": "https://www.tiktok.com/@tizio/video/123",
            "videoUrlNoWaterMark": "https://cdn.example.com/v.mp4",
            "text": "Routine serale",
            "diggCount": 900,
            "playCount": 30000,
            "commentCount": 45,
            "authorMeta": {"name": "tizio", "fans": 5000},
        }
    )

    assert video is not None
    assert video.like_count == 900
    assert video.view_count == 30000
    assert video.comment_count == 45
    assert video.author_username == "tizio"


# =============================================================================
# 3. L'interfaccia comune
# =============================================================================


def test_i_due_canali_si_escludono() -> None:
    """Con entrambi valorizzati il bivio a valle sceglierebbe in silenzio."""
    with pytest.raises(ValidationError):
        ScraperResult(
            platform="youtube_shorts",
            youtube_url="https://youtube.com/shorts/abc",
            video_bytes_url="https://cdn.example.com/v.mp4",
        )


def test_l_adattatore_manda_in_passthrough_solo_youtube_entro_il_limite() -> None:
    """La stessa regola dello scraper, applicata ai video che il cron ha gia'."""
    settings = get_settings()
    breve = ScrapedVideo(
        video_url="https://youtube.com/shorts/abc",
        download_url="https://cdn.example.com/v.mp4",
        duration_seconds=30.0,
    )
    lungo = ScrapedVideo(
        video_url="https://youtube.com/shorts/abc",
        download_url="https://cdn.example.com/v.mp4",
        duration_seconds=settings.max_video_duration_seconds + 1,
    )
    tiktok = ScrapedVideo(
        video_url="https://tiktok.com/@tizio/video/123",
        download_url="https://cdn.example.com/v.mp4",
        duration_seconds=30.0,
    )

    assert da_video_scrapato(breve, "youtube_shorts", settings=settings).is_passthrough
    # Oltre il limite si scarica: cosi' il rifiuto arriva dal downloader, che
    # misura invece di fidarsi del metadato.
    assert not da_video_scrapato(lungo, "youtube_shorts", settings=settings).is_passthrough
    assert not da_video_scrapato(tiktok, "tiktok", settings=settings).is_passthrough


def test_il_cron_analizza_un_video_youtube_in_passthrough(
    client: TestClient, cron_headers: dict[str, str], store: FakeStore,
    apify: FakeApify, gemini: FakeGemini, downloads: list[str],
) -> None:
    """Il percorso che analizza piu' video di tutti e' anche quello che ci guadagna di piu'."""
    store.seed("creators", {
        "user_id": USER_ID, "username": "creator", "platform": "youtube_shorts",
        "analysis_mode": "INFO", "is_active": True,
    })
    apify.videos = [ScrapedVideo(
        video_url="https://youtube.com/shorts/dQw4w9WgXcQ",
        download_url="https://cdn.example.com/v.mp4",
        duration_seconds=45.0,
    )]

    risposta = client.post("/api/v1/cron/check-updates", headers=cron_headers)

    assert risposta.status_code == 200, risposta.text
    assert len(gemini.url_calls) == 1, "il cron ha scaricato invece di passare l'URL"
    assert downloads == []


# =============================================================================
# 4. Le due leve sui token
# =============================================================================


def test_la_modalita_info_campiona_meno_frame() -> None:
    """`INFO` non misura il montaggio: meno frame e frame piu' economici.

    E' l'unica modalita' per cui abbassare le due leve non toglie nulla a cio'
    che deve produrre — argomento, punti chiave, pubblico.
    """
    settings = get_settings()
    preset = preset_per_modalita("INFO", settings)

    assert preset.fps == settings.gemini_video_fps_ridotto
    assert preset.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW


@pytest.mark.parametrize("mode", ["STYLE", "BOTH"])
def test_le_modalita_che_guardano_il_montaggio_restano_a_frame_rate_pieno(mode: str) -> None:
    """Durata dell'hook e lunghezza dell'inquadratura si perdono campionando rado."""
    settings = get_settings()
    preset = preset_per_modalita(mode, settings)  # type: ignore[arg-type]

    assert preset.fps == settings.gemini_video_fps
    assert preset.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_MEDIUM


def test_il_pavimento_dell_fps_e_imposto_dalla_configurazione(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sotto 0.3 FPS lo short-form perde i suoi beat: il limite non e' un consiglio."""
    from pydantic import ValidationError as PydanticValidationError

    from app.core.config import Settings

    monkeypatch.setenv("GEMINI_VIDEO_FPS", "0.05")
    with pytest.raises(PydanticValidationError):
        Settings()


def test_il_part_youtube_non_dichiara_il_mime_type() -> None:
    """Su un URL YouTube il tipo non lo sappiamo, e non e' compito nostro."""
    part = build_video_part(YOUTUBE_URL, mime_type=None, fps=0.5)

    assert part.file_data is not None
    assert part.file_data.file_uri == YOUTUBE_URL
    assert part.file_data.mime_type is None
    assert part.video_metadata is not None
    assert part.video_metadata.fps == 0.5


def test_il_part_di_un_file_caricato_dichiara_il_mime_type() -> None:
    part = build_video_part("files/abc", mime_type="video/mp4", fps=1.0)

    assert part.file_data is not None
    assert part.file_data.mime_type == "video/mp4"
    assert part.video_metadata is not None
    assert part.video_metadata.fps == 1.0
