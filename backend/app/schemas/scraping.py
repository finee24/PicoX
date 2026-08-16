"""Il risultato di uno scraping, uguale per tutte le piattaforme.

È il tipo che disaccoppia *come si ottiene un video* da *come lo si analizza*.
A valle esiste una sola funzione — `run_analysis` in `services/analysis_service.py` — e si
rammifica su **un solo bivio**:

* `youtube_url` valorizzato → l'URL va passato a Gemini così com'è, che lo
  scarica lato Google. Nessun byte transita da noi;
* `video_bytes_url` valorizzato → il file va scaricato e caricato sulla Files
  API, perché per Instagram e TikTok un passthrough nativo non esiste.

I due campi si escludono a vicenda ed è un invariante verificato, non una
convenzione: se un giorno entrambi fossero valorizzati, il bivio a valle
sceglierebbe in silenzio e la ragione della scelta non sarebbe più leggibile in
nessun punto del codice.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.creators import Platform


class ScraperResult(BaseModel):
    """Un video, come lo vede il resto dell'applicazione."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform = Field(
        description="Piattaforma di provenienza, nella nomenclatura del progetto."
    )

    youtube_url: str | None = Field(
        default=None,
        description=(
            "URL da passare direttamente a Gemini (`file_data`). Solo YouTube: "
            "è l'unica piattaforma per cui il modello scarica da sé."
        ),
    )
    video_bytes_url: str | None = Field(
        default=None,
        description=(
            "URL del file media da scaricare e caricare sulla Files API. Scade: "
            "non va mai persistito né usato come chiave."
        ),
    )

    caption: str = Field(
        default="",
        description="Testo del post. Stringa vuota se il video non ne ha uno.",
    )
    like_count: int | None = Field(default=None, ge=0)
    view_count: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    author_username: str = Field(
        default="",
        description="Handle di chi ha pubblicato, senza '@'. Vuoto se non ricavabile.",
    )
    posted_at: datetime | None = Field(default=None)

    # --- Campi oltre l'interfaccia comune, e perché ci sono ------------------
    # Non fanno parte del "cosa mostrare" ma del "cosa serve per analizzare":
    # senza, l'informazione andrebbe recuperata una seconda volta o andrebbe
    # persa fra lo scraping e la scrittura dell'insight.
    thumbnail_url: str | None = Field(
        default=None,
        description="Copertina, persistita in `insights.thumbnail_url`.",
    )
    duration_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Durata nota prima del download. Sul passthrough YouTube è **l'unico** "
            "modo di applicare `MAX_VIDEO_DURATION_SECONDS`: non scaricando il "
            "file, non c'è nulla da misurare a valle."
        ),
    )

    @model_validator(mode="after")
    def _un_solo_canale(self) -> ScraperResult:
        if self.youtube_url and self.video_bytes_url:
            raise ValueError(
                "youtube_url e video_bytes_url si escludono: un video si analizza "
                "per passthrough oppure per byte, non entrambi."
            )
        return self

    @property
    def is_passthrough(self) -> bool:
        """`True` se l'analisi non richiede di scaricare nulla."""
        return bool(self.youtube_url)
