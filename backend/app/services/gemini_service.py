"""Analisi multimodale del video con Gemini in structured output.

Due modi di dare un video al modello, e cambia solo la prima riga del flusso:

* **byte** (Instagram, TikTok) — upload sulla Files API, attesa dello stato
  `ACTIVE`, riferimento al file caricato;
* **passthrough** (YouTube) — l'URL viene passato in `file_data` e a scaricarlo
  è Google. Nessun byte transita da noi, nessun file da cancellare dopo.

Da lì in poi il percorso è lo stesso:

1. `generate_content` con `response_mime_type="application/json"` e
   `response_schema=VideoAnalysisResponse`;
2. validazione Pydantic della risposta — **prima** che qualcosa raggiunga il
   database.

## Due retry, a due altezze diverse

Si sommano male se si confondono, quindi vale distinguerli:

* **sul trasporto** — `HttpRetryOptions` sul client (`gemini_retry_attempts`).
  Copre 429/503/timeout, cioè gli errori che Google dichiara temporanei. Vale per
  *ogni* chiamata, `files.upload` e `files.get` comprese;
* **sul parsing** — il ciclo in `_generate_with_retry`, **un solo retry**: il
  modello occasionalmente tronca il JSON e un secondo tentativo lo risolve.

Il secondo non copre il primo: su `APIError` quel ciclo solleva subito, per
scelta — ritentare l'inferenza intera quando è la rete a non funzionare
significherebbe pagare due volte il video. Quando falliscono entrambi si solleva
`GeminiError` (503) e sul database non viene scritto nulla.

I due si **moltiplicano** nel caso peggiore, ed è il numero che
`analysis_lock_ttl_seconds` deve coprire: vedi il commento in `config.py`.

Il testo del prompt non sta qui: lo compone `prompt_loader.build_analysis_prompt`
a partire da `prompts/analysis_prompt.yaml`. Questo modulo decide *come* guardare
il video (fps, risoluzione, passthrough), non *cosa* chiedergli.

## Ridurre i token senza toccare i byte

Il video non viene pre-elaborato da noi: niente ffmpeg, niente estrazione di
frame. Le due leve sono native della chiamata e si sommano.

* `video_metadata.fps` sul Part — quanti frame al secondo il modello campiona.
  Ogni frame è tokenizzato una volta, quindi il costo scala **linearmente**:
  metà frame rate, metà token dei frame.
* `media_resolution` sulla config di generazione — quanti token vale un frame:
  ~258 a risoluzione media, ~66 a `LOW`, indipendentemente dall'fps.

Verificato contro `google-genai` 1.75.0, che è la versione installata:
`GenerateContentConfig.media_resolution` esiste (per richiesta) e
`VideoMetadata.fps` accetta un valore in `(0.0, 24.0]`, default 1.0. Nel Part
esiste anche un `media_resolution` per parte, ma la documentazione ufficiale
descrive quello per richiesta, e con un solo video per richiesta i due
coinciderebbero comunque.

Le regole complete sono in `.claude/skills/gemini-structured-output/SKILL.md`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import GeminiError
from app.schemas.analysis import AnalysisMode, VideoAnalysisResponse
from app.services.prompt_loader import AnalysisContext, build_analysis_prompt

logger = logging.getLogger(__name__)

_FILE_POLL_INTERVAL_SECONDS: Final = 2.0

# Tentativi sulla *risposta fuori schema*, non sul trasporto (quello è
# `gemini_retry_attempts`, sul client). Costante e non impostazione perché non è
# una leva da girare in produzione: se il modello sbaglia lo schema due volte non
# è il campionamento a essere sbagliato. Vive qui perché il caso peggiore del
# lock si calcola moltiplicando i due, e un `2` letterale dentro il ciclo
# renderebbe quel calcolo — e il test che lo verifica — una coincidenza.
_TENTATIVI_SCHEMA: Final = 2


@dataclass(frozen=True, slots=True)
class PresetVideo:
    """Quanto guardare un video, per una data modalità di analisi."""

    fps: float
    media_resolution: types.MediaResolution


def preset_per_modalita(mode: AnalysisMode, settings: Settings) -> PresetVideo:
    """Preset legato alla modalità già scelta dall'utente nel selettore.

    Le tre modalità non sono un'etichetta di velocità ma di *contenuto*, e da lì
    discende quanta risoluzione temporale serve davvero:

    * `STYLE` e `BOTH` misurano il montaggio — durata dell'hook, lunghezza media
      dell'inquadratura, tecniche di editing. Sono esattamente le cose che un
      campionamento rado non vede: qui si resta al frame rate pieno;
    * `INFO` estrae argomento, punti chiave e pubblico, che stanno nel parlato,
      nel testo a schermo e nella scena. Dimezzare i frame e scendere a `LOW`
      taglia i token di circa un ordine di grandezza senza togliere nulla a ciò
      che quella modalità deve produrre.

    I valori arrivano da `Settings`, quindi sono regolabili per preset da
    variabile d'ambiente senza toccare il codice.
    """
    if mode == "INFO":
        return PresetVideo(
            fps=settings.gemini_video_fps_ridotto,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        )
    return PresetVideo(
        fps=settings.gemini_video_fps,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    )


def build_video_part(file_uri: str, *, mime_type: str | None, fps: float) -> types.Part:
    """Il Part del video, uguale per i due percorsi tranne che nel `mime_type`.

    Su un URL YouTube il tipo **non** va dichiarato: il contenuto non è un file
    che abbiamo caricato noi, e a risolverlo è Google. Su un file della Files API
    invece serve, ed è quello con cui è stato caricato.
    """
    return types.Part(
        file_data=types.FileData(file_uri=file_uri, mime_type=mime_type),
        video_metadata=types.VideoMetadata(fps=fps),
    )


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
                timeout=self._settings.gemini_timeout_seconds * 1000,
                # Senza questo il client **non ritenta nulla**: il default di
                # `google-genai` è `stop_after_attempt(1)`. Un `503` di capacità
                # — che Google dichiara temporaneo nel messaggio stesso —
                # terminava l'analisi al primo colpo, e il ciclo qui sotto in
                # `_generate_with_retry` non lo copre: quello ritenta la
                # *risposta fuori schema*, e su `APIError` solleva subito.
                #
                # Sta sul client e non attorno alla singola chiamata perché così
                # protegge anche `files.upload` e `files.get`, che hanno lo
                # stesso identico problema e nessun ciclo attorno.
                retry_options=types.HttpRetryOptions(
                    attempts=self._settings.gemini_retry_attempts,
                    initial_delay=1.0,
                    # Tetto basso di proposito: il caso che si vuole assorbire è
                    # un picco di domanda che rientra in secondi, non un guasto
                    # prolungato. Attese lunghe qui verrebbero pagate dal lock,
                    # non dal servizio remoto.
                    max_delay=8.0,
                ),
            ),
        )

    async def analyze_video(
        self,
        video_path: str,
        mime_type: str,
        mode: AnalysisMode,
        *,
        context: AnalysisContext | None = None,
    ) -> VideoAnalysisResponse:
        """Analizza un video già scaricato in locale.

        Il file remoto viene cancellato in ogni caso: successo, errore di
        inferenza o eccezione durante la validazione.
        """
        preset = preset_per_modalita(mode, self._settings)
        uploaded = await self._upload_and_wait(video_path, mime_type)
        try:
            part = build_video_part(
                uploaded.uri or "", mime_type=uploaded.mime_type, fps=preset.fps
            )
            return await self._generate_with_retry(part, mode, preset, context)
        finally:
            await self._delete_remote_file(uploaded.name)

    async def analyze_video_url(
        self,
        video_url: str,
        mode: AnalysisMode,
        *,
        context: AnalysisContext | None = None,
    ) -> VideoAnalysisResponse:
        """Analizza un video YouTube senza scaricarlo.

        L'URL viaggia in `file_data` e il download lo fa Google: nessun byte
        passa da qui, nessun file resta sulla Files API da cancellare — e
        infatti manca il `finally` che c'è nell'altro percorso, perché non c'è
        nulla da ripulire.

        **Solo YouTube.** Non è una restrizione nostra: è l'unica sorgente che
        il modello sa risolvere da sé. Per Instagram e TikTok un URL passato qui
        verrebbe rifiutato, e il chiamante deve mandare i byte.
        """
        preset = preset_per_modalita(mode, self._settings)
        # Nessun `mime_type`: su un URL YouTube dichiararlo è un errore, il
        # contenuto non è un file caricato da noi.
        part = build_video_part(video_url, mime_type=None, fps=preset.fps)
        return await self._generate_with_retry(part, mode, preset, context)

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
        self,
        video: types.Part,
        mode: AnalysisMode,
        preset: PresetVideo,
        context: AnalysisContext | None = None,
    ) -> VideoAnalysisResponse:
        """L'inferenza, identica per i due percorsi.

        Prende un `Part` già costruito e non sa se dietro ci sia un file
        caricato o un URL YouTube: è il punto in cui le due strade si
        ricongiungono, ed è ciò che permette di averne una sola da qui in giù.
        """
        prompt = build_analysis_prompt(mode, context)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VideoAnalysisResponse,
            temperature=0.4,
            # Quanto vale un frame in token: ~258 a media risoluzione, ~66 a
            # `LOW`. Si somma alla riduzione dell'fps, che agisce invece su
            # *quanti* frame ci sono.
            media_resolution=preset.media_resolution,
        )

        last_error: Exception | None = None

        # Un solo retry: se il modello ha sbagliato due volte, il problema non è
        # il campionamento e ritentare all'infinito costa soltanto token.
        for attempt in range(1, _TENTATIVI_SCHEMA + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=[video, prompt],
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
