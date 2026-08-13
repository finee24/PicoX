"""Schemi dei creator monitorati (`public.creators`)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.analysis import AnalysisMode

Platform = Literal["instagram", "tiktok", "youtube_shorts"]
"""Allineata al CHECK constraint di `creators.platform`."""

# Unione permissiva dei charset ammessi dalle tre piattaforme: lettere, cifre,
# punto, underscore, trattino. La `@` iniziale viene rimossa dal validator.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def clean_username(value: str) -> str:
    """Handle senza '@' e senza spazi, o `ValueError` se non è un handle.

    Pubblica perché è la stessa regola che deve valere sull'handle estratto da
    un URL di profilo (`creator_validation`): due definizioni di "username
    valido" divergerebbero, e la prima conseguenza sarebbe un identificatore
    accettato dalla validazione e rifiutato dalla creazione del creator.
    """
    username = value.strip().lstrip("@")
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "Username non valido: sono ammessi solo lettere, cifre, punto, "
            "underscore e trattino (max 80 caratteri)."
        )
    return username


class CreatorCreate(BaseModel):
    """Body di `POST /api/v1/creators`."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=1,
        max_length=81,
        description="Handle del creator sulla piattaforma, con o senza '@'.",
    )
    platform: Platform = Field(description="Piattaforma su cui il creator pubblica.")
    analysis_mode: AnalysisMode = Field(
        default="BOTH",
        description="Modalità applicata di default ai video di questo creator.",
    )
    is_active: bool = Field(
        default=True,
        description="Se false il creator viene escluso dal cron senza perdere lo storico.",
    )

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        return clean_username(value)


class CreatorUpdate(BaseModel):
    """Body di `PATCH /api/v1/creators/{id}` — solo i campi modificabili."""

    model_config = ConfigDict(extra="forbid")

    analysis_mode: AnalysisMode | None = Field(
        default=None, description="Nuova modalità di analisi. Omesso = invariato."
    )
    is_active: bool | None = Field(
        default=None, description="Nuovo stato del monitoraggio. Omesso = invariato."
    )

    @model_validator(mode="after")
    def _at_least_one_field(self) -> CreatorUpdate:
        if self.analysis_mode is None and self.is_active is None:
            raise ValueError("Specificare almeno uno tra 'analysis_mode' e 'is_active'.")
        return self

    def changed_fields(self) -> dict[str, Any]:
        """Solo i campi effettivamente inviati dal client."""
        return self.model_dump(exclude_none=True)


class CreatorResponse(BaseModel):
    """Rappresentazione pubblica di un creator.

    Funge da filtro d'uscita: una colonna aggiunta al database in futuro non
    raggiunge il client finché non viene dichiarata qui.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    username: str
    platform: Platform
    analysis_mode: AnalysisMode
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CreatorListResponse(BaseModel):
    items: list[CreatorResponse] = Field(description="Creator dell'utente autenticato.")
    total: int = Field(description="Numero totale di creator dell'utente.")


# =============================================================================
# Validazione di un account (`POST /api/v1/creators/validate`)
# =============================================================================


# Le piattaforme si nominano `instagram`, `tiktok` e `youtube_shorts` in tutto
# il progetto: è il CHECK constraint di `creators.platform`, il tipo `Platform`
# qui sopra e l'enum del frontend. La validazione usa la stessa parola, così il
# valore che l'endpoint restituisce si può passare tale e quale a
# `POST /api/v1/creators` senza tradurlo.
#
# `youtube` viene però **accettato in ingresso** come sinonimo: è la forma
# naturale da scrivere per chi chiama l'API a mano, e rifiutarla con un 422
# sarebbe una scortesia senza contropartita. La traduzione avviene qui, in un
# punto solo, e da qui in giù esiste un nome unico.
_ALIAS_PIATTAFORMA: dict[str, Platform] = {
    "youtube": "youtube_shorts",
    "youtube_short": "youtube_shorts",
    "instagram": "instagram",
    "tiktok": "tiktok",
    "youtube_shorts": "youtube_shorts",
}


class CreatorValidationRequest(BaseModel):
    """Body di `POST /api/v1/creators/validate`."""

    model_config = ConfigDict(extra="forbid")

    input: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "Handle (`@nome`, `nome`) oppure URL del profilo. Il limite di 300 "
            "caratteri è largo per un handle e stretto per un URL costruito ad arte."
        ),
    )
    platform: Platform | None = Field(
        default=None,
        description=(
            "Piattaforma. Omessa, viene dedotta dall'host dell'URL. Un handle "
            "nudo non è deducibile — `@tizio` esiste su tutte e tre — e in quel "
            "caso l'assenza produce un 422. Accetta anche `youtube` come "
            "sinonimo di `youtube_shorts`."
        ),
    )

    @field_validator("platform", mode="before")
    @classmethod
    def _normalize_platform(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _ALIAS_PIATTAFORMA.get(value.strip().lower(), value)
        return value


class CreatorProfilePreview(BaseModel):
    """Anteprima del profilo, per la card mostrata prima di aggiungere.

    Come ogni modello di risposta di questo progetto fa da **filtro d'uscita**:
    i payload degli actor Apify e della YouTube Data API contengono molto altro
    — email di contatto, business category, id interni — e nulla di tutto ciò
    raggiunge il client, perché non è dichiarato qui.
    """

    avatar_url: str | None = Field(
        default=None, description="URL dell'immagine di profilo, se disponibile."
    )
    display_name: str = Field(
        description="Nome mostrato. Ricade sull'username quando il provider non lo dà."
    )
    username: str = Field(description="Handle normalizzato, senza '@'.")
    is_verified: bool = Field(
        default=False,
        description=(
            "Spunta di verifica. `false` anche quando la piattaforma non espone "
            "l'informazione: è un'assenza di conferma, non una smentita."
        ),
    )
    follower_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Follower per Instagram e TikTok, iscritti per YouTube: stessa "
            "metrica concettuale. `0` quando il profilo li nasconde."
        ),
    )

    @field_validator("avatar_url", mode="before")
    @classmethod
    def _only_http_urls(cls, value: Any) -> Any:
        """Scarta gli URL che non sono http(s).

        L'avatar arriva da un provider esterno e finisce in un `src` nel
        browser. Un `data:` o uno schema esotico non ha alcun uso legittimo qui,
        e non è compito del frontend accorgersene: si scarta all'ingresso, dove
        il valore entra nel sistema.
        """
        if isinstance(value, str) and not value.lower().startswith(("http://", "https://")):
            return None
        return value


class CreatorValidationResponse(BaseModel):
    """Esito della verifica di un account."""

    platform: Platform = Field(description="Piattaforma effettivamente interrogata.")
    normalized_identifier: str = Field(
        description=(
            "Handle estratto e normalizzato: minuscolo, senza '@' e senza URL "
            "attorno. È il valore da passare a `POST /api/v1/creators`, ed è la "
            "chiave con cui la verifica viene messa in cache."
        )
    )
    exists: bool = Field(description="L'account è stato trovato sulla piattaforma.")
    is_public: bool = Field(
        description=(
            "L'account è consultabile senza seguirlo. Un profilo privato esiste "
            "ma non è analizzabile: `exists=true, is_public=false`."
        )
    )
    profile: CreatorProfilePreview | None = Field(
        default=None,
        description=(
            "Anteprima, quando c'è qualcosa da mostrare. Assente se l'account "
            "non esiste; presente anche per un profilo privato, che espone "
            "comunque nome e avatar."
        ),
    )
    checked_at: datetime = Field(
        description=(
            "Istante dell'ultima verifica **reale** contro il provider. Su una "
            "risposta servita dalla cache è quello, non l'ora della richiesta: "
            "è ciò che permette al client di sapere quanto è vecchio il dato."
        )
    )
