"""Middleware ASGI che apre il contesto di logging di ogni richiesta.

È scritto come middleware ASGI puro e non come `BaseHTTPMiddleware` di Starlette
per una ragione precisa: `BaseHTTPMiddleware` esegue la richiesta in un task
separato e rompe la propagazione delle `contextvars` verso i `BackgroundTasks`.
`POST /api/v1/cron/check-updates` accoda proprio lì le analisi dei video, e con
`BaseHTTPMiddleware` quelle righe di log perderebbero il `request_id` — cioè
esattamente il caso in cui la correlazione serve di più, perché il lavoro
prosegue dopo che la risposta è già stata inviata.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.observability import (
    request_id_var,
    sanitize_incoming_request_id,
    user_id_var,
)

logger = logging.getLogger("app.access")

_REQUEST_ID_HEADER = b"x-request-id"

# Render richiama `/health` di continuo: a INFO l'access log diventerebbe quasi
# solo quello, seppellendo il traffico vero.
_QUIET_PATHS = frozenset({"/health"})


def _incoming_request_id(scope: Scope) -> str | None:
    """Riusa l'id del proxy a monte, se ne ha propagato uno valido.

    Permette di seguire la stessa richiesta attraverso più servizi. Il valore è
    validato in `sanitize_incoming_request_id`: è pur sempre un header sotto il
    controllo del chiamante.
    """
    for name, value in scope.get("headers", ()):
        if name == _REQUEST_ID_HEADER:
            return sanitize_incoming_request_id(value.decode("latin-1", "replace"))
    return None


class RequestContextMiddleware:
    """Assegna un `request_id` a ogni richiesta HTTP ed emette l'access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid4().hex
        rid_token = request_id_var.set(request_id)
        # Reimpostata esplicitamente: senza, una richiesta anonima servita dal
        # medesimo contesto erediterebbe l'utente della precedente.
        uid_token = user_id_var.set(None)

        started = time.perf_counter()
        path: str = scope.get("path", "")
        method: str = scope.get("method", "")

        async def send_wrapper(message: Message) -> None:
            # L'access log si emette qui e non alla fine di `self.app`: per gli
            # endpoint con BackgroundTasks quest'ultimo ritorna solo a lavoro
            # finito, e la durata registrata non sarebbe più quella percepita
            # dal client.
            if message["type"] == "http.response.start":
                elapsed_ms = (time.perf_counter() - started) * 1000
                status = message["status"]
                extra: dict[str, Any] = {
                    "http_method": method,
                    "http_path": path,
                    "http_status": status,
                    "duration_ms": round(elapsed_ms, 2),
                }
                level = logging.DEBUG if path in _QUIET_PATHS else logging.INFO
                if status >= 500:
                    level = logging.ERROR
                logger.log(
                    level, "%s %s -> %s (%.0f ms)", method, path, status, elapsed_ms,
                    extra=extra,
                )
                # Nessun header `X-Request-ID` in uscita: l'id resta interno.
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(rid_token)
            user_id_var.reset(uid_token)
