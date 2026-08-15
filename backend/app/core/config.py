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

    # Tentativi HTTP per chiamata a Gemini, **incluso il primo**. Non è il retry
    # sullo schema (quello vive in `_generate_with_retry` e ne bastano 2): è la
    # difesa contro gli errori di trasporto e di capacità, che Google restituisce
    # come 429/503 dichiarandoli temporanei.
    #
    # Serve perché senza `retry_options` il client `google-genai` non ritenta
    # **nulla** — il suo default è `stop_after_attempt(1)`. Misurato il 15 agosto
    # 2026 su `gemini-flash-latest`: 1 richiesta su 6 tornava
    # `503 "experiencing high demand"` in ~4s, e ognuna faceva fallire l'analisi
    # dell'utente. Sul percorso con download il costo era già stato pagato
    # (Apify + trasferimento) e si buttava via.
    #
    # 3 e non di più per il vincolo del lock: ogni tentativo può durare fino a
    # `gemini_timeout_seconds`, e il prodotto con i 2 tentativi sullo schema
    # entra nel caso peggiore che `analysis_lock_ttl_seconds` deve coprire.
    gemini_retry_attempts: int = Field(default=3, ge=1, le=5)

    # Frame al secondo campionati dal modello. È la leva più diretta sui token:
    # ogni frame è tokenizzato una volta, quindi il costo scala linearmente col
    # frame rate. Il default dell'API è 1.0.
    #
    # **Non scendere sotto 0.3-0.5 per il contenuto short-form**: Reel, Shorts e
    # TikTok sono montati con tagli rapidi, e sotto quella soglia l'analisi dello
    # stile perde i beat visivi che deve misurare — la durata dell'hook, la
    # lunghezza media dell'inquadratura. Il validatore impone il pavimento.
    gemini_video_fps: float = Field(default=1.0, ge=0.3, le=24.0)
    # Frame rate delle analisi che non guardano il montaggio (modalità `INFO`):
    # lì contano parlato, testo a schermo e argomento, che un campionamento più
    # rado non perde.
    gemini_video_fps_ridotto: float = Field(default=0.5, ge=0.3, le=24.0)

    # --- Apify ---------------------------------------------------------------
    apify_api_token: SecretStr
    apify_instagram_actor: str = "apify/instagram-scraper"
    apify_tiktok_actor: str = "clockworks/tiktok-scraper"
    apify_youtube_actor: str = "streamers/youtube-scraper"
    apify_timeout_seconds: int = 180
    # Quanti video recenti richiedere per creator a ogni giro di cron.
    apify_results_per_creator: int = 10
    # Actor dedicato al *profilo* Instagram, distinto da quello sui post: quello
    # sopra scrapa i contenuti di un URL e non espone `isPrivate`, che è
    # esattamente il campo su cui la validazione decide.
    apify_instagram_profile_actor: str = "apify/instagram-profile-scraper"
    # Timeout più stretto di `apify_timeout_seconds`: un profilo è una singola
    # pagina, non un dataset di video, e questa chiamata sta dentro una
    # richiesta HTTP interattiva — l'utente sta guardando un input in loading.
    apify_profile_timeout_seconds: int = Field(default=45, ge=5)

    # --- YouTube Data API v3 --------------------------------------------------
    # Solo per la validazione dei canali (`channels.list`): lo scraping dei
    # video resta su Apify. Facoltativa — senza, la validazione YouTube risponde
    # 503 e le altre due piattaforme continuano a funzionare.
    youtube_api_key: SecretStr | None = None
    youtube_api_timeout_seconds: int = Field(default=15, ge=1)

    # --- Validazione dei creator ---------------------------------------------
    # Quanto resta valida una riga di `creator_validations` prima di essere
    # rivalidata. È una scelta di freschezza contro costo: un profilo che passa
    # da pubblico a privato resta visto come pubblico al più per questa durata,
    # e in cambio non si ripaga il provider a ogni chiamata.
    creator_validation_ttl_hours: int = Field(default=24, ge=1)

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
    # pagare due volte. Il caso peggiore è la somma dei timeout a valle:
    #
    #   Apify 180 + download 120 + attesa del file Gemini 120
    #   + inferenza 300 per 3 tentativi HTTP per 2 tentativi sullo schema = 2220s
    #
    # I due retry si **moltiplicano**, non si sommano: ogni tentativo sullo
    # schema è una `generate_content` che al suo interno può ritentare fino a
    # `gemini_retry_attempts` volte. È il motivo per cui questo numero è salito
    # da 1200 a 2400 il 15 agosto 2026, insieme all'introduzione del retry HTTP:
    # lasciarlo a 1200 avrebbe reintrodotto la doppia spesa che la migration
    # `0003` esiste per chiudere, e in silenzio.
    #
    # `tests/test_timeout_gemini.py` verifica l'aritmetica: se qualcuno alza un
    # timeout a valle o i tentativi, il test fallisce invece di lasciar
    # scoperchiare il lock in silenzio.
    #
    # Il costo di alzarlo è che un processo morto a metà analisi tiene occupata
    # quella tripla per 40 minuti invece di 20. È un blocco per (utente, video,
    # modalità), non globale, e la richiesta che lo trova preso risponde 409
    # dopo `analysis_lock_wait_seconds` invece di restare appesa.
    analysis_lock_ttl_seconds: int = Field(default=2400, ge=60)
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
