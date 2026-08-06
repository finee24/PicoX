"""`GET /api/v1/insights` — feed degli insight con filtri e paginazione.

Lettura standard dell'utente: client scoped al JWT, RLS attivo.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from postgrest import CountMethod

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.middleware.error_handler import SafeRoute
from app.schemas.insights import InsightFilterMode, InsightListResponse, InsightResponse
from app.services.supabase_service import db_errors, scoped_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["insights"], route_class=SafeRoute)

# Caratteri che romperebbero il parser dei filtri PostgREST: la lista `or=(...)`
# è separata da virgole e delimitata da parentesi, `{}` delimita i literal di
# array e `*` è il jolly di `ilike`. Rimuoverli evita che un termine di ricerca
# possa alterare la struttura della query.
_SEARCH_UNSAFE = re.compile(r'[,()"\\{}*]')
_SEARCH_MAX_LENGTH = 100


def _sanitize_search(term: str) -> str:
    return _SEARCH_UNSAFE.sub(" ", term).strip()[:_SEARCH_MAX_LENGTH]


@router.get(
    "/insights",
    response_model=InsightListResponse,
    summary="Elenca gli insight dell'utente con ricerca, filtri e paginazione",
    responses={
        401: {"description": "JWT assente o non valido."},
        422: {"description": "Parametri di query non validi."},
    },
)
async def list_insights(
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    search: Annotated[
        str | None,
        Query(
            max_length=200,
            description=(
                "Testo da cercare fra le keyword e nei campi testuali di `summary_data`."
            ),
        ),
    ] = None,
    mode: Annotated[
        InsightFilterMode | None,
        Query(
            description=(
                "INFO/STYLE/BOTH filtrano su `analysis_mode`. SCRIPT seleziona i record "
                "che hanno uno script inverso, qualunque sia la modalità di analisi."
            )
        ),
    ] = None,
    creator_id: Annotated[
        UUID | None, Query(description="Limita ai video di un singolo creator.")
    ] = None,
    page: Annotated[int, Query(ge=1, description="Pagina, 1-based.")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Risultati per pagina.")] = 20,
) -> InsightListResponse:
    """Feed degli insight dell'utente autenticato, dal più recente.

    La paginazione è reale (offset/limit lato database): `total` arriva dal
    `count` esatto di PostgREST, non dalla lunghezza della pagina.
    """
    offset = (page - 1) * limit

    async with db_errors("select insights"):
        async with scoped_client(user.access_token, settings) as db:
            query = (
                db.table("insights")
                .select("*", count=CountMethod.exact)
                .eq("user_id", user.id)
            )

            if creator_id is not None:
                query = query.eq("creator_id", str(creator_id))

            if mode is not None:
                if mode == "SCRIPT":
                    # Filtro sulla *presenza* dello script, non sulla modalità:
                    # un record BOTH senza script non deve comparire, e in futuro
                    # uno script potrebbe essere generato anche altrove.
                    query = query.not_.is_("inverse_script_template", "null")
                else:
                    query = query.eq("analysis_mode", mode)

            if search:
                term = _sanitize_search(search)
                if term:
                    # `keywords.cs.{term}` sfrutta l'indice GIN ma richiede la
                    # keyword esatta; gli `ilike` coprono le corrispondenze
                    # parziali sui campi testuali del payload INFO.
                    # Evoluzione naturale quando la tabella cresce: una colonna
                    # tsvector generata su summary_data + keywords con indice GIN,
                    # e qui un solo `text_search`.
                    query = query.or_(
                        ",".join(
                            (
                                f"keywords.cs.{{{term}}}",
                                f"summary_data->>summary.ilike.*{term}*",
                                f"summary_data->>main_topic.ilike.*{term}*",
                                f"summary_data->>value_proposition.ilike.*{term}*",
                            )
                        )
                    )

            result = await (
                query.order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )

    items = [InsightResponse.model_validate(row) for row in result.data or []]
    total = result.count if result.count is not None else len(items)

    return InsightListResponse(
        items=items,
        page=page,
        limit=limit,
        total=total,
        has_more=offset + len(items) < total,
    )
