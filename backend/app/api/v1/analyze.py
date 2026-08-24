"""`POST /api/v1/analyze-video` — analisi di un singolo video.

Qui resta solo ciò che riguarda l'HTTP: come si legge la richiesta, quale status
si restituisce, quali risposte sono documentate. La pipeline vera —
normalizzazione, cache, lock, spesa esterna, scrittura — vive in
`app.services.analysis_service`, dov'è raggiungibile anche dal job cron senza
che un job pianificato debba importare da un modulo di route.

Il 200 contro il 201 non è un dettaglio di forma: distingue un record servito
dalla cache da un'analisi appena pagata, ed è l'unico modo che il client ha di
saperlo senza confrontare i timestamp.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.middleware.error_handler import SafeRoute
from app.schemas.analysis import AnalyzeVideoRequest
from app.schemas.insights import InsightResponse
from app.services.analysis_service import perform_analysis
from app.services.apify_service import ApifyService, get_apify_service
from app.services.gemini_service import GeminiService, get_gemini_service
from app.services.media_service import normalize_video_url
from app.services.youtube_service import YouTubeService, get_youtube_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["analysis"], route_class=SafeRoute)


@router.post(
    "/analyze-video",
    response_model=InsightResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Analizza un video e restituisce l'insight",
    responses={
        200: {"description": "Video già analizzato: restituito il record in cache."},
        401: {"description": "JWT assente o non valido."},
        409: {
            "description": (
                "Un'altra richiesta sta già analizzando questo video per lo stesso "
                "utente e non ha finito entro l'attesa massima."
            )
        },
        422: {"description": "URL non valido, oppure video oltre i limiti di dimensione o durata."},
        503: {"description": "Gemini o Apify non disponibili."},
    },
)
async def analyze_video(
    payload: AnalyzeVideoRequest,
    user: CurrentUser,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    gemini: Annotated[GeminiService, Depends(get_gemini_service)],
    apify: Annotated[ApifyService, Depends(get_apify_service)],
    youtube: Annotated[YouTubeService, Depends(get_youtube_service)],
) -> InsightResponse:
    """Analizza il video indicato per conto dell'utente autenticato.

    `user_id` viene dal JWT verificato: il body non lo contiene e non potrebbe
    comunque influenzarlo.
    """
    video_url = normalize_video_url(payload.video_url)

    record, from_cache = await perform_analysis(
        user_id=user.id,
        video_url=video_url,
        mode=payload.analysis_mode,
        settings=settings,
        gemini=gemini,
        apify=apify,
        youtube=youtube,
        access_token=user.access_token,
    )

    # 201 solo quando l'analisi è stata effettivamente eseguita.
    response.status_code = status.HTTP_200_OK if from_cache else status.HTTP_201_CREATED
    return record
