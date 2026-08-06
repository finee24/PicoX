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


def _clean_username(value: str) -> str:
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
        return _clean_username(value)


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
    def _at_least_one_field(self) -> "CreatorUpdate":
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
