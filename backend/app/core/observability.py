"""Logging strutturato e contesto della richiesta.

Il problema che risolve: in produzione un errore su Gemini o Apify si manifesta
come una riga di log isolata, senza modo di collegarla alla richiesta che l'ha
provocata né all'utente che l'ha subita. Con decine di richieste concorrenti,
`grep` sul messaggio non basta per ricostruire una singola storia.

Ogni riga di log porta quindi due campi, presi da `contextvars` popolate una
volta per richiesta:

* `request_id` — identificativo della richiesta HTTP;
* `user_id` — UUID dell'utente, quando la richiesta è autenticata.

Essendo `contextvars`, il valore segue automaticamente ogni `await` e ogni task
figlio: le analisi accodate dal cron restano correlate alla richiesta che le ha
generate senza passarsi l'id di funzione in funzione.

**Il `request_id` non viene mai restituito al client.** Nessun header di
risposta, nessun campo nel body d'errore: serve alla correlazione lato server e
basta. Il costo è che un utente che segnala un problema non può citare un id —
per ritrovare la sua richiesta si parte da `user_id` e timestamp.

Due formati, scelti in base all'ambiente:

* sviluppo → riga leggibile a occhio in console;
* produzione → una riga JSON per record, indicizzabile dagli aggregatori di log
  (Render, Datadog, Grafana Loki) senza dover scrivere pattern di parsing.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

# =============================================================================
# Contesto della richiesta
# =============================================================================

request_id_var: ContextVar[str | None] = ContextVar("picox_request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("picox_user_id", default=None)

# Un id che arriva dall'esterno è input non fidato come qualsiasi altro: senza
# questo vincolo un client può iniettare newline nei log (spezzando una riga in
# due record falsi) o spingerci dentro stringhe arbitrariamente lunghe. Il
# pattern ammette solo ciò che un proxy genera davvero.
_SAFE_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def sanitize_incoming_request_id(value: str | None) -> str | None:
    """Accetta l'id propagato da un proxy solo se ha una forma plausibile."""
    if value and _SAFE_REQUEST_ID.match(value):
        return value
    return None


def current_request_id() -> str | None:
    return request_id_var.get()


def bind_user_id(user_id: str | None) -> None:
    """Aggancia l'utente al contesto corrente.

    Chiamata dopo la verifica del JWT: prima di quel punto l'identità è solo
    un'affermazione del client, e loggarla darebbe una correlazione falsa.
    """
    user_id_var.set(user_id)


# =============================================================================
# Logging
# =============================================================================


class RequestContextFilter(logging.Filter):
    """Copia il contesto della richiesta su ogni `LogRecord`.

    Un filtro invece di un `LoggerAdapter`: così vale anche per i log emessi
    dalle librerie di terze parti (uvicorn, supabase, google-genai), che non
    passano dal nostro codice ma finiscono nello stesso stream.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.user_id = user_id_var.get() or "-"
        return True


# Attributi standard di LogRecord: tutto ciò che non è in questa lista è stato
# passato dal chiamante via `extra=` e va incluso nel JSON.
_RESERVED_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        "request_id", "user_id",
    }
)


class JsonLogFormatter(logging.Formatter):
    """Un record per riga in JSON, pronto per l'ingestione automatica."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            # Lo stack trace resta nei log — è l'unico posto in cui deve stare.
            # Le risposte HTTP passano da `middleware.error_handler`, che non lo
            # include mai.
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # `default=str` evita che un valore non serializzabile passato per errore
        # via `extra=` faccia esplodere il logging stesso.
        return json.dumps(payload, ensure_ascii=False, default=str)


_DEV_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] — %(message)s"


def configure_logging(level: str, *, json_output: bool) -> None:
    """Configura il root logger. Idempotente: sostituisce gli handler esistenti."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonLogFormatter() if json_output else logging.Formatter(_DEV_FORMAT)
    )
    # Il filtro va sull'handler, non sul logger: i filtri dei logger non sono
    # ereditati dai figli, quelli degli handler si applicano a tutto ciò che
    # arriva all'handler — comprese le librerie esterne.
    handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # httpx logga ogni richiesta con l'URL completo: verso Supabase e Gemini
    # quell'URL può contenere parametri che non vogliamo nei log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # hpack e h2 sono il livello sotto: a DEBUG stampano la tabella di
    # compressione degli header HTTP/2, e lì dentro passa in chiaro l'header
    # `apikey` — cioè la service_role key verso Supabase. Silenziare httpx non
    # basta, perché questi logger sono fratelli e non figli.
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("h2").setLevel(logging.WARNING)
    # L'access log di uvicorn duplicherebbe quello di RequestContextMiddleware,
    # ma senza request_id e user_id.
    logging.getLogger("uvicorn.access").disabled = True
