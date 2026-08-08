"""Analisi multimodale del video con Gemini in structured output.

Il flusso per ogni video:

1. upload del file locale sulla File API;
2. attesa che il file passi in stato `ACTIVE` (l'inferenza su un file ancora in
   `PROCESSING` fallisce);
3. `generate_content` con `response_mime_type="application/json"` e
   `response_schema=VideoAnalysisResponse`;
4. validazione Pydantic della risposta — **prima** che qualcosa raggiunga il
   database;
5. cancellazione del file remoto, garantita da un `finally`.

Sul parsing è previsto **un solo retry**: il modello occasionalmente tronca il
JSON, e un secondo tentativo lo risolve. Se fallisce anche quello si solleva
`GeminiError` (503) e sul database non viene scritto nulla.

Le regole complete sono in `.claude/skills/gemini-structured-output/SKILL.md`.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path
from typing import Final

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import GeminiError
from app.schemas.analysis import AnalysisMode, VideoAnalysisResponse

logger = logging.getLogger(__name__)

# I prompt vivono fuori dal codice applicativo: si iterano senza toccare i
# service (vedi il subagent .claude/agents/prompt-tuner.md).
_PROMPTS_DIR: Final = Path(__file__).resolve().parents[2] / "prompts"

_FILE_POLL_INTERVAL_SECONDS: Final = 2.0


@lru_cache(maxsize=8)
def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GeminiError(details=None) from exc


def build_prompt(mode: AnalysisMode) -> str:
    """Compone il prompt con le sole sezioni pertinenti alla modalità.

    Il modello riceve istruzioni solo per ciò che deve produrre; le sezioni
    escluse sono elencate esplicitamente come `null` attesi, perché uno schema
    con un campo nullable non basta a impedire che venga riempito.
    """
    sections = [_load_prompt("base")]
    expected_null: list[str] = []

    if mode in ("INFO", "BOTH"):
        sections.append(_load_prompt("info"))
    else:
        expected_null.append("info_analysis")

    if mode in ("STYLE", "BOTH"):
        sections.append(_load_prompt("style"))
    else:
        expected_null.append("style_analysis")

    if mode == "BOTH":
        sections.append(_load_prompt("script"))
    else:
        expected_null.append("inverse_script")

    if expected_null:
        fields = ", ".join(f"`{name}`" for name in expected_null)
        sections.append(
            f"## Campi da lasciare vuoti\n\n"
            f"Questa analisi è in modalità **{mode}**. I campi {fields} devono essere "
            f"`null`. Non compilarli in nessun caso, nemmeno parzialmente."
        )

    return "\n\n---\n\n".join(sections)


class GeminiService:
    """Client Gemini per l'analisi video."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = genai.Client(
            api_key=self._settings.gemini_api_key.get_secret_value(),
            # Senza questo, `gemini_timeout_seconds` era una promessa che nessuno
            # manteneva: la variabile esisteva in configurazione e non veniva
            # letta da nessuna parte. Una `generate_content` che non risponde
            # tiene occupati la richiesta e il worker uvicorn a tempo
            # indefinito — e su un'inferenza multimodale non è un caso di
            # scuola.
            #
            # `HttpOptions.timeout` è in **millisecondi**, mentre la nostra
            # configurazione è in secondi: la conversione è il motivo per cui
            # questa riga non è un semplice passaggio di parametro.
            http_options=types.HttpOptions(
                timeout=self._settings.gemini_timeout_seconds * 1000
            ),
        )

    async def analyze_video(
        self,
        video_path: str,
        mime_type: str,
        mode: AnalysisMode,
    ) -> VideoAnalysisResponse:
        """Analizza un video già scaricato in locale.

        Il file remoto viene cancellato in ogni caso: successo, errore di
        inferenza o eccezione durante la validazione.
        """
        uploaded = await self._upload_and_wait(video_path, mime_type)
        try:
            return await self._generate_with_retry(uploaded, mode)
        finally:
            await self._delete_remote_file(uploaded.name)

    # --- Upload --------------------------------------------------------------

    async def _upload_and_wait(self, video_path: str, mime_type: str) -> types.File:
        try:
            uploaded = await self._client.aio.files.upload(
                file=video_path,
                config=types.UploadFileConfig(mime_type=mime_type),
            )
        except genai_errors.APIError as exc:
            logger.error("Gemini: upload del file fallito (status=%s)", exc.code, exc_info=True)
            raise GeminiError() from exc
        except Exception as exc:
            logger.error("Gemini: upload del file fallito.", exc_info=True)
            raise GeminiError() from exc

        if not uploaded.name:
            raise GeminiError()

        return await self._wait_until_active(uploaded)

    async def _wait_until_active(self, uploaded: types.File) -> types.File:
        """Attende il passaggio in `ACTIVE`, con timeout.

        In caso di timeout o di stato `FAILED` il file remoto viene rimosso
        subito: non ha senso lasciarlo occupare quota per 48 ore.
        """
        assert uploaded.name is not None
        deadline = (
            asyncio.get_running_loop().time()
            + self._settings.gemini_file_active_timeout_seconds
        )
        current = uploaded

        while current.state == types.FileState.PROCESSING:
            if asyncio.get_running_loop().time() >= deadline:
                await self._delete_remote_file(uploaded.name)
                logger.error("Gemini: il file non è diventato ACTIVE entro il timeout.")
                raise GeminiError(
                    "L'elaborazione del video ha superato il tempo massimo. Riprova."
                )
            await asyncio.sleep(_FILE_POLL_INTERVAL_SECONDS)
            try:
                current = await self._client.aio.files.get(name=uploaded.name)
            except Exception as exc:
                await self._delete_remote_file(uploaded.name)
                logger.error("Gemini: polling dello stato del file fallito.", exc_info=True)
                raise GeminiError() from exc

        if current.state != types.FileState.ACTIVE:
            await self._delete_remote_file(uploaded.name)
            logger.error("Gemini: file in stato terminale inatteso (%s).", current.state)
            raise GeminiError("Il video non è stato elaborato correttamente.")

        return current

    # --- Inferenza -----------------------------------------------------------

    async def _generate_with_retry(
        self, uploaded: types.File, mode: AnalysisMode
    ) -> VideoAnalysisResponse:
        prompt = build_prompt(mode)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VideoAnalysisResponse,
            temperature=0.4,
        )

        last_error: Exception | None = None

        # Un solo retry: se il modello ha sbagliato due volte, il problema non è
        # il campionamento e ritentare all'infinito costa soltanto token.
        for attempt in (1, 2):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=[
                        types.Part.from_uri(
                            file_uri=uploaded.uri or "", mime_type=uploaded.mime_type
                        ),
                        prompt,
                    ],
                    config=config,
                )
            except genai_errors.APIError as exc:
                logger.error(
                    "Gemini: generate_content fallito (tentativo %s, status=%s)",
                    attempt,
                    exc.code,
                    exc_info=True,
                )
                raise GeminiError() from exc
            except Exception as exc:
                logger.error(
                    "Gemini: generate_content fallito (tentativo %s).", attempt, exc_info=True
                )
                raise GeminiError() from exc

            try:
                return self._parse_response(response, mode)
            except (ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "Gemini: risposta non conforme allo schema (tentativo %s/2): %s",
                    attempt,
                    type(exc).__name__,
                )

        logger.error("Gemini: risposta non validabile dopo il retry.", exc_info=last_error)
        raise GeminiError(
            "L'analisi non ha prodotto un risultato valido. Riprova tra qualche minuto."
        ) from last_error

    def _parse_response(
        self, response: types.GenerateContentResponse, mode: AnalysisMode
    ) -> VideoAnalysisResponse:
        """Valida la risposta e verifica la coerenza con la modalità richiesta."""
        raw = response.text
        if not raw or not raw.strip():
            raise ValueError("Risposta vuota dal modello.")

        # Validazione obbligatoria prima di qualsiasi scrittura su Supabase.
        analysis = VideoAnalysisResponse.model_validate_json(raw)

        # Le sezioni richieste devono esserci: una risposta con tutti i campi a
        # `null` supera lo schema ma non è un'analisi.
        if mode in ("INFO", "BOTH") and analysis.info_analysis is None:
            raise ValueError("Sezione info_analysis mancante per la modalità richiesta.")
        if mode in ("STYLE", "BOTH") and analysis.style_analysis is None:
            raise ValueError("Sezione style_analysis mancante per la modalità richiesta.")
        if mode == "BOTH" and analysis.inverse_script is None:
            raise ValueError("Sezione inverse_script mancante per la modalità BOTH.")

        # Le sezioni non richieste vengono scartate anche se il modello le ha
        # compilate: il record deve riflettere la modalità effettivamente eseguita.
        if mode == "STYLE":
            analysis.info_analysis = None
        if mode == "INFO":
            analysis.style_analysis = None
        if mode != "BOTH":
            analysis.inverse_script = None

        if not analysis.keywords and analysis.info_analysis is not None:
            analysis.keywords = list(analysis.info_analysis.keywords)

        return analysis

    # --- Cleanup -------------------------------------------------------------

    async def _delete_remote_file(self, name: str | None) -> None:
        """Rimuove il file dalla File API.

        Non propaga mai: un cleanup fallito non deve trasformare un'analisi
        riuscita in un errore, né mascherare l'eccezione originale se stiamo già
        risalendo uno stack in errore.
        """
        if not name:
            return
        try:
            await self._client.aio.files.delete(name=name)
        except Exception:
            logger.warning("Gemini: impossibile cancellare il file remoto.", exc_info=True)


_gemini_service: GeminiService | None = None


def get_gemini_service() -> GeminiService:
    """Istanza condivisa (usabile come dependency FastAPI)."""
    global _gemini_service
    if _gemini_service is None:
        _gemini_service = GeminiService()
    return _gemini_service
