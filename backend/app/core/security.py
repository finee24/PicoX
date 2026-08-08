"""Autenticazione: verifica del JWT Supabase e del segreto del cron.

Due strategie di verifica della firma, scelte in base alla configurazione:

1. **HS256 con `SUPABASE_JWT_SECRET`** — progetti con il JWT secret legacy.
   Verifica puramente locale, nessun round-trip di rete.
2. **JWKS asimmetrico** — progetti con signing key ES256/RS256. Le chiavi
   pubbliche sono scaricate una volta e tenute in cache da `PyJWKClient`.

In entrambi i casi la verifica è *locale*: nessuna chiamata a GoTrue nel percorso
critico di ogni richiesta. Un token non verificabile è sempre `401`, senza
distinguere il motivo verso il client (scaduto / firma errata / malformato
sarebbero altrettanti bit di informazione per chi sta sondando l'API).
"""

from __future__ import annotations

import hmac
import logging
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError
from app.core.observability import bind_user_id

logger = logging.getLogger(__name__)

# `auto_error=False`: gestiamo noi la risposta 401 tramite AuthError, così il
# corpo dell'errore ha la stessa forma di tutti gli altri errori dell'API.
bearer_scheme = HTTPBearer(auto_error=False, description="JWT Supabase dell'utente")


class AuthenticatedUser(BaseModel):
    """Identità ricavata dal JWT verificato.

    `id` è l'unica fonte ammessa per lo scoping delle query: nessun endpoint
    deve accettare uno `user_id` proveniente dal body, dalla query string o da
    un path param.
    """

    id: str = Field(description="UUID dell'utente (claim `sub` del JWT)")
    email: str | None = Field(default=None, description="Email dell'utente, se presente nel token")
    role: str | None = Field(default=None, description="Ruolo Postgres associato al token")

    # Il token viaggia con l'utente per costruire il client Supabase scoped.
    # Escluso da ogni serializzazione: non deve finire in una risposta o in un log.
    access_token: str = Field(repr=False, exclude=True)


@lru_cache(maxsize=1)
def _get_jwk_client(jwks_url: str) -> jwt.PyJWKClient:
    """Client JWKS con cache delle chiavi (evita un fetch per richiesta)."""
    return jwt.PyJWKClient(jwks_url, cache_keys=True, max_cached_keys=8)


def _decode_hs256(token: str, settings: Settings) -> dict[str, Any]:
    assert settings.supabase_jwt_secret is not None
    return jwt.decode(
        token,
        settings.supabase_jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.supabase_jwt_audience,
        options={"require": ["exp", "sub"]},
    )


def _decode_jwks(token: str, settings: Settings) -> dict[str, Any]:
    signing_key = _get_jwk_client(settings.jwks_url).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256", "RS256", "EdDSA"],
        audience=settings.supabase_jwt_audience,
        options={"require": ["exp", "sub"]},
    )


def _decode(token: str, settings: Settings) -> dict[str, Any]:
    if settings.supabase_jwt_secret is not None:
        return _decode_hs256(token, settings)
    return _decode_jwks(token, settings)


async def verify_supabase_jwt(token: str, settings: Settings) -> AuthenticatedUser:
    """Verifica firma, scadenza e audience del token; restituisce l'identità."""
    try:
        # Il percorso JWKS può fare I/O di rete al primo token (poi è in cache):
        # va fuori dall'event loop per non bloccare le altre richieste.
        claims = await run_in_threadpool(_decode, token, settings)
    except jwt.PyJWTError as exc:
        # Il motivo resta nei log; al client va un 401 indifferenziato.
        logger.info("Rifiutato JWT non valido: %s", type(exc).__name__)
        raise AuthError("Token non valido o scaduto.") from exc
    except Exception as exc:  # rete/JWKS non raggiungibile
        logger.warning("Verifica JWT non riuscita: %s", type(exc).__name__)
        raise AuthError("Impossibile verificare le credenziali.") from exc

    subject = claims.get("sub")
    if not subject:
        # Stesso messaggio degli altri fallimenti: distinguere "firma valida ma
        # senza sub" da "firma non valida" direbbe al chiamante che il token è
        # stato emesso da questo progetto.
        logger.info("Rifiutato JWT valido ma privo del claim 'sub'.")
        raise AuthError("Token non valido o scaduto.")

    # Da qui in avanti ogni riga di log della richiesta porta l'utente. Il bind
    # avviene *dopo* la verifica della firma: agganciare il `sub` di un token
    # non verificato produrrebbe log che attribuiscono attività a un utente che
    # non ha fatto nulla.
    bind_user_id(str(subject))

    return AuthenticatedUser(
        id=str(subject),
        email=claims.get("email"),
        role=claims.get("role"),
        access_token=token,
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedUser:
    """Dependency di autenticazione per tutte le route protette."""
    if credentials is None or not credentials.credentials:
        raise AuthError("Header Authorization mancante.")
    if credentials.scheme.lower() != "bearer":
        raise AuthError("Schema di autenticazione non supportato.")
    return await verify_supabase_jwt(credentials.credentials, settings)


async def verify_cron_secret(
    settings: Annotated[Settings, Depends(get_settings)],
    x_cron_secret: Annotated[str | None, Header(alias="X-CRON-SECRET")] = None,
) -> None:
    """Autentica lo scheduler tramite segreto condiviso.

    Il confronto è in tempo costante: un `==` su stringhe permette di dedurre il
    segreto un carattere alla volta misurando i tempi di risposta.
    """
    if not x_cron_secret:
        raise AuthError("Header X-CRON-SECRET mancante.")

    expected = settings.cron_secret.get_secret_value()
    # Confronto sui byte: `compare_digest` su `str` solleva TypeError se l'header
    # contiene caratteri non-ASCII, e un 500 al posto di un 401 è già un bit di
    # informazione per chi sta sondando l'endpoint.
    if not expected or not hmac.compare_digest(
        x_cron_secret.encode("utf-8"), expected.encode("utf-8")
    ):
        logger.warning("Chiamata al cron rifiutata: X-CRON-SECRET errato.")
        raise AuthError("Segreto del cron non valido.")


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
"""Alias da usare nelle firme delle route: `user: CurrentUser`."""
