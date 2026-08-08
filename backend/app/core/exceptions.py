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
