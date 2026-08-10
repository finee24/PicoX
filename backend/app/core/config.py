"""Configurazione applicativa.

Tutti i segreti sono `SecretStr`: il loro valore non compare mai in un `repr()`,
in un log strutturato o in una risposta di errore. L'accesso al valore reale
richiede sempre `.get_secret_value()` esplicito, che rende il punto di utilizzo
grep-abile in fase di review.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Variabili d'ambiente del backend Picox."""

    model_config = SettingsConfigDict(
        # `backend/.env` ha precedenza sul `.env` di root del monorepo.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Ambiente ------------------------------------------------------------
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")

    # --- Supabase ------------------------------------------------------------
    supabase_url: str
    supabase_anon_key: SecretStr
    supabase_service_role_key: SecretStr

    # Segreto HS256 legacy dei progetti Supabase (Settings -> API -> JWT Secret).
    # Se assente si verifica il JWT con le chiavi asimmetriche esposte da JWKS.
    supabase_jwt_secret: SecretStr | None = None
    supabase_jwt_audience: str = "authenticated"

    # --- Gemini --------------------------------------------------------------
    gemini_api_key: SecretStr
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: int = 300
    # Attesa massima perché il file caricato passi in stato ACTIVE.
    gemini_file_active_timeout_seconds: int = 120

    # --- Apify ---------------------------------------------------------------
    apify_api_token: SecretStr
    apify_instagram_actor: str = "apify/instagram-scraper"
    apify_tiktok_actor: str = "clockworks/tiktok-scraper"
    apify_youtube_actor: str = "streamers/youtube-scraper"
    apify_timeout_seconds: int = 180
    # Quanti video recenti richiedere per creator a ogni giro di cron.
    apify_results_per_creator: int = 10

    # --- Applicazione --------------------------------------------------------
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"
    cron_secret: SecretStr

    # --- Job cron ------------------------------------------------------------
    # Interruttore esplicito, spento per difetto. Senza, "il cron non è ancora
    # pronto" era vero solo per un commento in `cron_config.md`: chi non l'avesse
    # letto avrebbe committato lo scheduler e l'avrebbe scoperto dai costi.
    # Accenderlo è una modifica deliberata e visibile in diff.
    cron_enabled: bool = False
    # Durata del lock su un giro di cron. **Non è la durata massima di un giro**
    # — quella, nel caso pessimo, è di decine di ore — ma il limite di quanto
    # accettiamo di restare fermi per un processo morto a metà. Sta sopra un
    # giro realistico completo e sotto lo schedule di 6h di un fattore 12: se
    # scade durante un giro lungo si torna al comportamento precedente, non a
    # qualcosa di peggio, perché le analisi restano protette da `analysis_locks`.
    cron_lock_ttl_seconds: int = Field(default=1800, ge=60)
    # Creator scrapati in parallelo durante il censimento. Con il ciclo
    # sequenziale, al tetto di 30 creator attivi il censimento tipico sfiorava i
    # 120s — la soglia oltre la quale il client abortiva e ritentava, che era la
    # sorgente vera delle esecuzioni sovrapposte.
    cron_census_concurrency: int = Field(default=6, ge=1)

    # --- Limiti di ingestione video -----------------------------------------
    max_video_mb: int = Field(default=200, ge=1)
    max_video_duration_seconds: int = Field(default=600, ge=1)
    download_timeout_seconds: int = 120
    # Analisi in background eseguite in parallelo dal job cron.
    cron_max_concurrent_analyses: int = Field(default=2, ge=1)

    # --- Deduplica delle analisi concorrenti ---------------------------------
    # Durata del lock su (user_id, video_url, analysis_mode). **Deve superare la
    # durata massima di un'analisi legittima**: se scadesse prima, una seconda
    # richiesta lo sottrarrebbe a un'analisi ancora in corso e si tornerebbe a
    # pagare due volte. Il caso peggiore è la somma dei timeout a valle —
    # Apify 180 + download 120 + attesa del file Gemini 120 + inferenza 300 per
    # ciascuno dei 2 tentativi = 1020s — e questo default lascia margine.
    analysis_lock_ttl_seconds: int = Field(default=1200, ge=60)
    # Quanto attende una richiesta che trova il lock già preso, prima di
    # arrendersi con 409. È un numero di esperienza utente e non ha alcun
    # obbligo di coincidere col TTL: attendere costa un `await` su un poll, non
    # un thread occupato.
    analysis_lock_wait_seconds: int = Field(default=90, ge=0)
    analysis_lock_poll_seconds: float = Field(default=1.5, gt=0)

    @field_validator("supabase_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def max_video_bytes(self) -> int:
        return self.max_video_mb * 1024 * 1024

    @property
    def jwks_url(self) -> str:
        """Endpoint delle chiavi pubbliche per i progetti con JWT asimmetrici."""
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"

    @property
    def cors_origins(self) -> list[str]:
        """Origin ammessi dal CORS: solo il frontend configurato.

        In sviluppo il frontend gira spesso su `localhost` e `127.0.0.1`
        indifferentemente: entrambe le forme vengono ammesse per non costringere
        a due configurazioni diverse. In produzione l'elenco resta il solo
        `FRONTEND_URL`.
        """
        origin = self.frontend_url.rstrip("/")
        if not origin:
            return []
        origins = [origin]
        if not self.is_production:
            if "localhost" in origin:
                origins.append(origin.replace("localhost", "127.0.0.1"))
            elif "127.0.0.1" in origin:
                origins.append(origin.replace("127.0.0.1", "localhost"))
        return origins

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Istanza singleton delle impostazioni (usata anche come dependency FastAPI)."""
    # Nessun `type: ignore` necessario: il plugin mypy di pydantic sa che i
    # campi senza default arrivano dall'ambiente e non dal costruttore.
    return Settings()
