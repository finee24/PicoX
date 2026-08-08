"""Fixture condivise.

Principio di questa suite: **nessuna chiamata di rete, nessuna credenziale
reale**. Le variabili d'ambiente sono impostate qui sopra, prima di qualsiasi
import di `app`, perché in pydantic-settings l'ambiente ha la precedenza sul
file `.env` — così la suite gira identica sulla macchina di chi sviluppa (dove
`backend/.env` esiste e contiene chiavi vere) e in CI (dove non esiste affatto).

Sull'autenticazione: i test **non** sostituiscono `get_current_user` con un
override. Firmano un JWT HS256 vero e lo mandano nell'header, così ogni test
attraversa `verify_supabase_jwt` per davvero — firma, scadenza, audience,
claim `sub`. Un override renderebbe verdi anche i test di `401`, che sono
proprio quelli da cui dipende la tenuta dell'API.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

# --- Ambiente di test (prima di importare `app`) -----------------------------
# Almeno 32 byte: sotto quella soglia PyJWT emette un warning su ogni firma.
TEST_JWT_SECRET = "test-jwt-secret-not-a-real-one-0123456789"
TEST_SERVICE_ROLE_KEY = "test-service-role-key"
TEST_ANON_KEY = "test-anon-key"
TEST_CRON_SECRET = "test-cron-secret"
TEST_FRONTEND_URL = "http://localhost:3000"

os.environ.update(
    {
        "ENVIRONMENT": "test",
        # INFO e non WARNING: l'access log è emesso a INFO, e i test sulla
        # correlazione dei log non avrebbero nulla da osservare a un livello
        # più alto.
        "LOG_LEVEL": "INFO",
        "SUPABASE_URL": "https://project.supabase.co",
        "SUPABASE_ANON_KEY": TEST_ANON_KEY,
        "SUPABASE_SERVICE_ROLE_KEY": TEST_SERVICE_ROLE_KEY,
        # Presente solo nei test: forza il percorso HS256, che è verificabile in
        # locale. Il percorso JWKS richiederebbe di raggiungere Supabase.
        "SUPABASE_JWT_SECRET": TEST_JWT_SECRET,
        "GEMINI_API_KEY": "test-gemini-key",
        "APIFY_API_TOKEN": "test-apify-token",
        "CRON_SECRET": TEST_CRON_SECRET,
        "FRONTEND_URL": TEST_FRONTEND_URL,
        "BACKEND_URL": "http://localhost:8000",
    }
)

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.schemas.analysis import (
    AnalysisMode,
    InfoAnalysis,
    InverseScript,
    ScriptBeat,
    StyleAnalysis,
    VideoAnalysisResponse,
)
from app.services.apify_service import ScrapedVideo
from app.services.media_service import DownloadedVideo
from tests.fake_supabase import FakeStore, make_acreate_client, make_jwt

USER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_USER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """`get_settings` è `lru_cache`: senza reset i test si passano lo stato."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# =============================================================================
# Database
# =============================================================================


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """Database in memoria, agganciato al posto di Supabase.

    Sostituisce `acreate_client`, unico punto da cui passano sia il client
    scoped sia quello service-role. Azzera anche il singleton del client
    service-role: senza, un test erediterebbe quello costruito dal precedente.
    """
    fake_store = FakeStore()

    import app.services.supabase_service as supabase_service

    monkeypatch.setattr(supabase_service, "_service_client", None)
    monkeypatch.setattr(
        supabase_service,
        "acreate_client",
        make_acreate_client(fake_store, TEST_SERVICE_ROLE_KEY),
    )
    return fake_store


# =============================================================================
# Servizi esterni
# =============================================================================


def build_analysis(mode: AnalysisMode) -> VideoAnalysisResponse:
    """Risposta di Gemini conforme allo schema, per la modalità richiesta."""
    info = InfoAnalysis(
        summary="Il video spiega come impostare una routine di studio serale.",
        main_topic="routine di studio serale",
        key_points=["Fissare un orario", "Eliminare le notifiche", "Ripassare 10 minuti"],
        target_audience="Studenti universitari al primo anno.",
        value_proposition="Imparare a studiare la sera senza perdere concentrazione.",
        content_format="tutorial",
        keywords=["studio", "produttività", "routine", "concentrazione", "università"],
    )
    style = StyleAnalysis(
        hook="Stai studiando nel momento sbagliato della giornata.",
        hook_type="bold_claim",
        hook_duration_seconds=2.5,
        pacing="fast",
        average_shot_duration_seconds=1.8,
        editing_techniques=["jump cut", "zoom progressivo"],
        visual_style="Inquadrature ravvicinate alla scrivania, luce calda.",
        text_overlay_usage="Parole chiave enfatizzate al centro dello schermo.",
        audio_style="Voce fuori campo con musica lo-fi di sottofondo.",
        tone_of_voice="diretto e confidenziale",
        cta="Salva il video per stasera.",
        retention_devices=["promessa risolta nel finale"],
    )
    script = InverseScript(
        title="Errore comune, causa nascosta, metodo in 3 passi",
        total_duration_seconds=42.0,
        beats=[
            ScriptBeat(
                timestamp="0:00-0:03",
                section="hook",
                content="Afferma che l'orario di studio è sbagliato.",
                purpose="Crea dissonanza e trattiene lo spettatore.",
                visual_direction="Primo piano frontale, taglio secco.",
            )
        ],
        reusable_hook_template="Stai {azione} nel momento sbagliato della giornata.",
        reusable_cta_template="Salva il video per {momento}.",
        adaptation_notes="Funziona su temi in cui il pubblico ha un'abitudine consolidata.",
    )

    return VideoAnalysisResponse(
        info_analysis=info if mode in ("INFO", "BOTH") else None,
        style_analysis=style if mode in ("STYLE", "BOTH") else None,
        inverse_script=script if mode == "BOTH" else None,
        keywords=list(info.keywords),
    )


class FakeGemini:
    """Doppio di `GeminiService` che conta le invocazioni.

    Il contatore è il cuore del test sulla cache: "non ha richiamato Gemini" è
    una proprietà osservabile, e senza contatore resterebbe un'affermazione non
    verificata.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.error: Exception | None = None

    async def analyze_video(
        self, video_path: str, mime_type: str, mode: AnalysisMode
    ) -> VideoAnalysisResponse:
        self.calls.append((video_path, mime_type, mode))
        if self.error is not None:
            raise self.error
        return build_analysis(mode)


class FakeApify:
    """Doppio di `ApifyService`."""

    def __init__(self) -> None:
        self.resolve_calls: list[str] = []
        self.fetch_calls: list[tuple[str, str]] = []
        self.resolved: ScrapedVideo | None = ScrapedVideo(
            video_url="https://tiktok.com/@creator/video/123",
            download_url="https://cdn.example.com/video-123.mp4",
            thumbnail_url="https://cdn.example.com/thumb-123.jpg",
            duration_seconds=42.0,
        )
        self.videos: list[ScrapedVideo] = []
        self.fetch_error: Exception | None = None

    async def resolve_video(
        self, video_url: str, platform: str | None = None
    ) -> ScrapedVideo | None:
        self.resolve_calls.append(video_url)
        return self.resolved

    async def fetch_latest_videos(
        self, platform: str, username: str, limit: int | None = None
    ) -> list[ScrapedVideo]:
        self.fetch_calls.append((platform, username))
        if self.fetch_error is not None:
            raise self.fetch_error
        return list(self.videos)


@pytest.fixture
def gemini() -> FakeGemini:
    return FakeGemini()


@pytest.fixture
def apify() -> FakeApify:
    return FakeApify()


@pytest.fixture(autouse=True)
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Neutralizza il download del media e registra gli URL richiesti.

    Serve anche come prova sulla cache: su un hit questa lista deve restare
    vuota, perché non si scarica nulla se non si analizza nulla.

    `autouse=True` di proposito. Da fixture opzionale, un test che si dimentica
    di richiederla non fallisce: fa una richiesta HTTP vera verso l'URL di
    esempio, diventa lento e dipendente dalla rete, e il motivo del fallimento
    (un 503 dal downloader) non ha niente a che vedere con ciò che il test
    voleva verificare. Chi ha bisogno di un download diverso lo sostituisce nel
    corpo del test: `monkeypatch` applicato lì ha comunque la precedenza.
    """
    requested: list[str] = []

    @asynccontextmanager
    async def fake_download(
        url: str, settings: Any, *, known_duration_seconds: float | None = None
    ) -> AsyncIterator[DownloadedVideo]:
        requested.append(url)
        yield DownloadedVideo(
            path="/tmp/picox-test.mp4",
            size_bytes=1024 * 1024,
            mime_type="video/mp4",
            duration_seconds=known_duration_seconds or 42.0,
        )

    import app.api.v1.analyze as analyze_module

    monkeypatch.setattr(analyze_module, "download_to_temp", fake_download)
    return requested


# =============================================================================
# Applicazione
# =============================================================================


@pytest.fixture
def app(gemini: FakeGemini, apify: FakeApify, store: FakeStore):
    """App FastAPI con i soli servizi esterni sostituiti.

    Auth, middleware, gestione degli errori e routing restano quelli di
    produzione: sono esattamente ciò che i test di hardening devono provare.
    """
    from app.main import create_app
    from app.services.apify_service import get_apify_service
    from app.services.gemini_service import get_gemini_service

    application = create_app()
    application.dependency_overrides[get_gemini_service] = lambda: gemini
    application.dependency_overrides[get_apify_service] = lambda: apify
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    # `raise_server_exceptions=False`: un 500 deve arrivare al test come
    # risposta HTTP da ispezionare, non come eccezione risollevata. Senza,
    # i test che verificano l'assenza di stack trace nel corpo non potrebbero
    # nemmeno leggere quel corpo.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(USER_ID, TEST_JWT_SECRET)}"}


@pytest.fixture
def other_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(OTHER_USER_ID, TEST_JWT_SECRET)}"}


@pytest.fixture
def cron_headers() -> dict[str, str]:
    return {"X-CRON-SECRET": TEST_CRON_SECRET}
