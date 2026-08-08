"""Gestione centralizzata degli errori.

Due meccanismi complementari:

* **`SafeRoute`** — un `APIRoute` che avvolge ogni handler in un try/except
  unico. È il "blocco globale per router": dentro le route si scrive il percorso
  felice e si sollevano eccezioni di dominio, senza `try/except Exception`
  sparsi a catturare l'ignoto.
* **`register_exception_handlers`** — traduce le eccezioni in risposte JSON con
  una forma unica.

Il contratto di errore è sempre:

```json
{ "error": { "code": "...", "message": "...", "details": ... } }
```

Nessuno stack trace, nessuna query, nessun URL interno e nessuna credenziale
raggiunge il client: il traceback completo finisce nei log del server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AuthError, PicoxError

logger = logging.getLogger(__name__)

# Mappa status -> code per le HTTPException sollevate da Starlette/FastAPI, che
# non hanno un `code` proprio.
_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "too_many_requests",
    503: "service_unavailable",
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


def _validation_details(exc: RequestValidationError | ValidationError) -> list[dict[str, Any]]:
    """Errori di validazione ridotti a campo + motivo.

    `input` e `ctx` sono volutamente esclusi: rimandano indietro il valore
    inviato, che può essere grande, e in un errore annidato può contenere
    frammenti non pertinenti.
    """
    details: list[dict[str, Any]] = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part != "body"]
        details.append(
            {
                "field": ".".join(location) or "body",
                "message": error.get("msg", "Valore non valido."),
                "type": error.get("type", "value_error"),
            }
        )
    return details


class SafeRoute(APIRoute):
    """Route con blocco try/except globale.

    Le eccezioni note passano oltre — gli handler registrati sull'app le mappano
    sugli status corretti. Qualunque altra cosa viene loggata con lo stack
    completo e restituita come 500 sanitizzato, così che un bug non trasformi
    mai un dettaglio interno in una risposta HTTP.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except (
                PicoxError,
                StarletteHTTPException,
                RequestValidationError,
                ValidationError,
            ):
                raise
            except Exception as exc:
                logger.exception(
                    "Errore non gestito in %s %s", request.method, request.url.path
                )
                raise PicoxError() from exc

        return safe_handler


def register_exception_handlers(app: FastAPI) -> None:
    """Registra gli handler che danno forma unica a tutte le risposte d'errore."""

    @app.exception_handler(PicoxError)
    async def _handle_picox_error(_: Request, exc: PicoxError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("[%s] %s", exc.code, exc.message, exc_info=True)

        headers = None
        if isinstance(exc, AuthError):
            # Il client deve poter distinguere "riautenticati" da un 401 generico.
            headers = {"WWW-Authenticate": "Bearer"}

        return error_response(
            exc.status_code, exc.code, exc.message, details=exc.details, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            422,
            "validation_error",
            "I dati della richiesta non sono validi.",
            details=_validation_details(exc),
        )

    @app.exception_handler(ValidationError)
    async def _handle_pydantic_validation(_: Request, exc: ValidationError) -> JSONResponse:
        # Validazione fallita nel nostro codice (es. payload di un servizio
        # esterno): per il client resta un 422, ma va tracciata come anomalia.
        logger.warning("Validazione interna fallita: %s", exc.error_count())
        return error_response(
            422,
            "validation_error",
            "I dati elaborati non sono validi.",
            details=_validation_details(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "Richiesta non valida."
        headers = getattr(exc, "headers", None)
        return error_response(exc.status_code, code, message, headers=headers)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Errore non gestito fuori dal router in %s %s", request.method, request.url.path
        )
        return error_response(
            500, "internal_error", "Si è verificato un errore imprevisto."
        )
