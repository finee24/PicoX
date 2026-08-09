"""Eccezioni di dominio.

Ogni eccezione porta con sé lo status HTTP e un `code` stabile: i router
sollevano queste, gli handler registrati in `app.middleware.error_handler` le
traducono in risposte. Il messaggio è pensato per essere mostrato a un utente
finale — non contiene mai stack trace, query, URL interni o credenziali.
"""

from __future__ import annotations

from typing import Any


class PicoxError(Exception):
    """Base di tutte le eccezioni applicative."""

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "Si è verificato un errore imprevisto."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class AuthError(PicoxError):
    """JWT assente, scaduto o non verificabile; segreto cron errato."""

    status_code = 401
    code = "unauthorized"
    default_message = "Autenticazione richiesta o non valida."


class ForbiddenError(PicoxError):
    status_code = 403
    code = "forbidden"
    default_message = "Operazione non consentita."


class NotFoundError(PicoxError):
    status_code = 404
    code = "not_found"
    default_message = "Risorsa non trovata."


class ConflictError(PicoxError):
    """Violazione di un vincolo di unicità (es. creator già monitorato)."""

    status_code = 409
    code = "conflict"
    default_message = "La risorsa esiste già."


class PlanLimitError(PicoxError):
    """L'utente ha raggiunto un limite previsto dal proprio piano.

    409 e non 403: non è una questione di permessi — l'operazione sarebbe
    legittima — ma di stato attuale della risorsa, che confligge con la
    richiesta. Nemmeno 402, che dichiarerebbe un paywall dove oggi c'è solo un
    tetto tecnico.

    Il messaggio è scritto qui e non ripreso dal database: quello di Postgres
    resta nei log, come per ogni altro errore tradotto in questo modulo.
    """

    status_code = 409
    code = "plan_limit_reached"
    default_message = (
        "Hai raggiunto il numero massimo di creator attivi previsto dal tuo "
        "piano. Disattivane o rimuovine uno per aggiungerne un altro."
    )


class AnalysisInProgressError(PicoxError):
    """Un'altra richiesta sta già analizzando questo video per questo utente.

    409 e non 425 o 503: la richiesta non è "troppo presto" né il servizio è
    indisponibile — è lo stato attuale della risorsa a rendere l'operazione
    superflua. Non è un errore da cui difendersi: l'analisi in corso arriverà a
    termine, e la richiesta successiva la troverà in cache.
    """

    status_code = 409
    code = "analysis_in_progress"
    default_message = (
        "Questo video è già in analisi. Riprova fra poco: il risultato sarà "
        "disponibile senza doverla rifare."
    )


class PicoxValidationError(PicoxError):
    """Input non valido: body malformato, video oltre i limiti consentiti."""

    status_code = 422
    code = "validation_error"
    default_message = "I dati forniti non sono validi."


class VideoTooLargeError(PicoxValidationError):
    code = "video_too_large"
    default_message = "Il video supera il limite di dimensione consentito."


class VideoTooLongError(PicoxValidationError):
    code = "video_too_long"
    default_message = "Il video supera la durata massima consentita."


class ExternalServiceError(PicoxError):
    """Fallimento di un provider esterno (Gemini, Apify, download del media).

    503 e non 500: la causa è fuori dal nostro processo ed è tipicamente
    transitoria, quindi il client può ritentare.
    """

    status_code = 503
    code = "external_service_error"
    default_message = "Servizio esterno temporaneamente non disponibile."

    def __init__(
        self,
        message: str | None = None,
        *,
        service: str | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.service = service


class GeminiError(ExternalServiceError):
    code = "gemini_unavailable"
    default_message = (
        "L'analisi del video non è al momento disponibile. Riprova tra qualche minuto."
    )

    def __init__(self, message: str | None = None, *, details: Any | None = None) -> None:
        super().__init__(message, service="gemini", details=details)


class ApifyError(ExternalServiceError):
    code = "apify_unavailable"
    default_message = "Il recupero dei video dal creator non è al momento disponibile."

    def __init__(self, message: str | None = None, *, details: Any | None = None) -> None:
        super().__init__(message, service="apify", details=details)


class DatabaseError(PicoxError):
    status_code = 503
    code = "database_unavailable"
    default_message = "Il servizio dati non è al momento disponibile."
