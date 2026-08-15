"""CRUD dei creator monitorati.

Tutte le operazioni usano il **client scoped al JWT**: sono azioni che l'utente
compie sui propri dati, quindi il RLS resta attivo e un filtro dimenticato
produce zero righe invece di una fuga di dati.

I filtri su `user_id` sono comunque scritti esplicitamente. Sono ridondanti
rispetto alle policy — ed è il punto: se un giorno una policy venisse allentata,
l'API non cambierebbe comportamento.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import CurrentUser
from app.middleware.error_handler import SafeRoute
from app.schemas.creators import (
    CreatorCreate,
    CreatorListResponse,
    CreatorResponse,
    CreatorUpdate,
    CreatorValidationRequest,
    CreatorValidationResponse,
)
from app.services.apify_service import ApifyService, get_apify_service
from app.services.creator_validation import validate_creator
from app.services.supabase_service import db_errors, scoped_client
from app.services.youtube_service import YouTubeService, get_youtube_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/creators", tags=["creators"], route_class=SafeRoute)


@router.post(
    "",
    response_model=CreatorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Aggiunge un creator alla watchlist",
    responses={
        401: {"description": "JWT assente o non valido."},
        409: {
            "description": (
                "Creator già presente per questo utente su questa piattaforma "
                "(`conflict`), oppure limite di creator attivi del piano "
                "raggiunto (`plan_limit_reached`)."
            )
        },
        422: {"description": "Dati del creator non validi."},
    },
)
async def create_creator(
    payload: CreatorCreate,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorResponse:
    """Crea un creator.

    L'unicità di `(user_id, username, platform)` è garantita dal constraint del
    database: affidarsi a un SELECT preventivo lascerebbe aperta la finestra fra
    controllo e insert. La violazione viene tradotta in 409.
    """
    record: dict[str, Any] = {
        "user_id": user.id,
        "username": payload.username,
        "platform": payload.platform,
        "analysis_mode": payload.analysis_mode,
        "is_active": payload.is_active,
    }

    async with db_errors("insert creator"), scoped_client(user.access_token, settings) as db:
        result = await db.table("creators").insert(record).execute()

    if not result.data:
        # Con il RLS attivo un insert che non torna nulla significa che la policy
        # ha rifiutato la riga.
        raise ConflictError("Impossibile creare il creator.")

    return CreatorResponse.model_validate(result.data[0])


@router.post(
    "/validate",
    response_model=CreatorValidationResponse,
    summary="Verifica che un account esista e sia pubblico",
    responses={
        401: {"description": "JWT assente o non valido."},
        409: {
            "description": (
                "Quota giornaliera di verifiche esaurita per il piano "
                "(`validation_quota_reached`)."
            )
        },
        422: {
            "description": (
                "Input non riconosciuto: link di un contenuto invece che di un "
                "profilo, host non supportato, oppure handle nudo senza `platform`."
            )
        },
        503: {"description": "Apify o la YouTube Data API non disponibili."},
    },
)
async def validate_creator_account(
    payload: CreatorValidationRequest,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    apify: Annotated[ApifyService, Depends(get_apify_service)],
    youtube: Annotated[YouTubeService, Depends(get_youtube_service)],
) -> CreatorValidationResponse:
    """Verifica un account prima che venga aggiunto alla watchlist.

    **200 anche quando l'account non esiste.** La verifica è andata a buon fine
    e la sua risposta è `exists: false`: un 404 direbbe che la *risorsa
    interrogata* non c'è — cioè l'endpoint — ed è un'altra affermazione.

    `user_id` viene dal JWT verificato: serve alla quota giornaliera, che è per
    utente. La cache invece è condivisa, perché il profilo di un creator non è
    un dato di nessuno (migration `0010`).
    """
    return await validate_creator(
        user_id=user.id,
        raw_input=payload.input,
        platform=payload.platform,
        settings=settings,
        apify=apify,
        youtube=youtube,
    )


@router.get(
    "",
    response_model=CreatorListResponse,
    summary="Elenca i creator dell'utente autenticato",
    responses={401: {"description": "JWT assente o non valido."}},
)
async def list_creators(
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorListResponse:
    """Tutti i creator dell'utente, dal più recente."""
    async with db_errors("select creators"), scoped_client(user.access_token, settings) as db:
        result = await (
            db.table("creators")
            .select("*")
            .eq("user_id", user.id)
            .order("created_at", desc=True)
            .execute()
        )

    items = [CreatorResponse.model_validate(row) for row in result.data or []]
    return CreatorListResponse(items=items, total=len(items))


@router.patch(
    "/{creator_id}",
    response_model=CreatorResponse,
    summary="Aggiorna modalità di analisi e stato di monitoraggio",
    responses={
        401: {"description": "JWT assente o non valido."},
        404: {"description": "Creator inesistente o non appartenente all'utente."},
        422: {"description": "Nessun campo aggiornabile fornito."},
    },
)
async def update_creator(
    creator_id: UUID,
    payload: CreatorUpdate,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorResponse:
    """Aggiorna `analysis_mode` e/o `is_active`.

    Solo questi due campi sono modificabili: `username` e `platform` fanno parte
    dell'identità della riga, cambiarli equivale a monitorare un altro creator.
    """
    changes = payload.changed_fields()

    async with db_errors("update creator"), scoped_client(user.access_token, settings) as db:
        result = await (
            db.table("creators")
            .update(changes)
            # La PK da sola non basta come filtro: `user_id` è ciò che rende
            # impossibile toccare la riga di un altro tenant.
            .eq("id", str(creator_id))
            .eq("user_id", user.id)
            .execute()
        )

    if not result.data:
        raise NotFoundError("Creator non trovato.")

    return CreatorResponse.model_validate(result.data[0])


@router.delete(
    "/{creator_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Rimuove un creator dalla watchlist",
    responses={
        204: {"description": "Creator eliminato."},
        401: {"description": "JWT assente o non valido."},
        404: {"description": "Creator inesistente o non appartenente all'utente."},
    },
)
async def delete_creator(
    creator_id: UUID,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Elimina il creator.

    Gli insight collegati sopravvivono: `insights.creator_id` è
    `ON DELETE SET NULL`, perché l'analisi è già stata pagata in token e resta
    di valore anche senza il creator di provenienza.
    """
    async with db_errors("delete creator"), scoped_client(user.access_token, settings) as db:
        result = await (
            db.table("creators")
            .delete()
            .eq("id", str(creator_id))
            .eq("user_id", user.id)
            .execute()
        )

    if not result.data:
        raise NotFoundError("Creator non trovato.")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
