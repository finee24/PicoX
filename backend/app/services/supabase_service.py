"""Accesso a Supabase con due client distinti e ruoli non intercambiabili.

## Client scoped al JWT (`scoped_client`)

Costruito con la **anon key** più il JWT dell'utente nell'header `Authorization`.
Il ruolo Postgres è `authenticated`, quindi **il RLS resta attivo**: un filtro
dimenticato produce zero righe, non una fuga di dati. È il client di default per
tutte le letture e per il CRUD che l'utente esegue su roba propria.

## Client service-role (`get_service_client`)

Costruito con la **service role key**. Il ruolo ha `BYPASSRLS`: nessuna policy
viene valutata. Riservato a due soli casi, entrambi privi di un JWT di sessione
utilizzabile:

1. la scrittura di `insights` dopo l'inferenza;
2. il job cron, che itera sui creator di *tutti* gli utenti.

Poiché qui il database non protegge più nulla, lo scoping è responsabilità del
codice. Invece di affidarlo alla disciplina di chi scrive la query, questo modulo
lo rende strutturale: il service-role non si usa mai direttamente sul client, ma
attraverso `service_table(client, tabella, user_id)`, che applica da sé
`.eq('user_id', ...)` a ogni SELECT/UPDATE/DELETE e lo inietta in ogni INSERT.

Il client service-role non viene mai esposto: `_get_service_client` è privato e
le uniche porte d'accesso sono `service_table` e `unscoped_service_table`. La
seconda è l'unica via per una query non filtrata, richiede una motivazione
esplicita, la registra nei log ed è limitata da un'allowlist di tabelle — oggi
la sola `creators`, per il giro del cron. Estenderla a un'altra tabella richiede
di modificare deliberatamente questo modulo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from postgrest import (
    AsyncFilterRequestBuilder,
    AsyncQueryRequestBuilder,
    AsyncRequestBuilder,
    AsyncSelectRequestBuilder,
    CountMethod,
)
from postgrest.exceptions import APIError
from supabase import AsyncClient, AsyncClientOptions, acreate_client

from app.core.config import Settings, get_settings
from app.core.exceptions import ConflictError, DatabaseError

logger = logging.getLogger(__name__)

# Codici Postgres che hanno una traduzione HTTP precisa.
_UNIQUE_VIOLATION: Final = "23505"
_FOREIGN_KEY_VIOLATION: Final = "23503"

# `profiles` è scopata sulla PK, non su una colonna `user_id`.
_SCOPE_COLUMN: Final[dict[str, str]] = {
    "profiles": "id",
    "creators": "user_id",
    "insights": "user_id",
    "analysis_locks": "user_id",
}

# Tabelle su cui è ammessa una query service-role non filtrata per utente.
# `creators` c'è perché il cron deve enumerare i creator attivi di tutti i
# tenant e non ha un utente corrente da cui derivare il filtro. `insights` non
# c'è, e non deve entrarci: lì una query non scopata è una fuga di dati.
_UNSCOPED_ALLOWED: Final[frozenset[str]] = frozenset({"creators"})

_service_client: AsyncClient | None = None
_service_client_lock = asyncio.Lock()


def _scope_column(table: str) -> str:
    try:
        return _SCOPE_COLUMN[table]
    except KeyError as exc:  # tabella nuova senza regola di scoping dichiarata
        raise RuntimeError(
            f"Nessuna colonna di scoping definita per la tabella '{table}': "
            "aggiungerla a _SCOPE_COLUMN prima di usarla col client service-role."
        ) from exc


# =============================================================================
# Client
# =============================================================================


async def _get_service_client() -> AsyncClient:
    """Client service-role, istanziato una sola volta per processo.

    **Privato di proposito.** Restituisce un client che bypassa il RLS: se fosse
    esportato, qualsiasi chiamante potrebbe fare `.table("insights").select("*")`
    e ottenere le righe di tutti i tenant senza lasciare traccia. Le uniche
    porte d'accesso sono `service_table` e `unscoped_service_table`.

    Non dipende dalla richiesta in corso, quindi può essere condiviso: riusare
    il pool di connessioni httpx evita un handshake TLS per ogni scrittura.
    """
    global _service_client
    if _service_client is None:
        async with _service_client_lock:
            if _service_client is None:
                settings = get_settings()
                _service_client = await acreate_client(
                    settings.supabase_url,
                    settings.supabase_service_role_key.get_secret_value(),
                    options=AsyncClientOptions(
                        auto_refresh_token=False,
                        persist_session=False,
                    ),
                )
                logger.info("Client Supabase service-role inizializzato.")
    return _service_client


async def close_service_client() -> None:
    """Chiude il client condiviso (chiamato allo shutdown dell'app)."""
    global _service_client
    if _service_client is not None:
        try:
            await _service_client.postgrest.aclose()
        except Exception:
            logger.debug("Chiusura del client service-role non riuscita.", exc_info=True)
        _service_client = None


@asynccontextmanager
async def scoped_client(
    access_token: str,
    settings: Settings | None = None,
) -> AsyncIterator[AsyncClient]:
    """Client legato al JWT dell'utente, con RLS attivo.

    Va creato per richiesta: l'header `Authorization` è parte della
    configurazione del client, quindi un'istanza condivisa significherebbe
    servire una richiesta con il token di un'altra.

    `supabase-py` compone gli header come `{**options.headers, **auth_headers}`
    dove `auth_headers` riprende l'`Authorization` già presente nelle options se
    c'è: il risultato è `apiKey: <anon>` + `Authorization: Bearer <jwt utente>`,
    che è esattamente la combinazione richiesta da PostgREST per applicare il RLS.
    """
    settings = settings or get_settings()
    client = await acreate_client(
        settings.supabase_url,
        settings.supabase_anon_key.get_secret_value(),
        options=AsyncClientOptions(
            headers={"Authorization": f"Bearer {access_token}"},
            auto_refresh_token=False,
            persist_session=False,
        ),
    )
    try:
        yield client
    finally:
        try:
            await client.postgrest.aclose()
        except Exception:
            logger.debug("Chiusura del client scoped non riuscita.", exc_info=True)


# =============================================================================
# Query service-role obbligatoriamente scopate
# =============================================================================


class ScopedServiceTable:
    """Builder che applica da sé il filtro sul proprietario a ogni operazione.

    Non è una comodità: è il punto in cui la regola "ogni query service-role
    filtra su `user_id`" smette di dipendere dalla memoria di chi scrive il
    codice. Non espone un accesso grezzo alla tabella.
    """

    __slots__ = ("_client", "_column", "_table", "_user_id")

    def __init__(self, client: AsyncClient, table: str, user_id: str) -> None:
        if not user_id:
            raise RuntimeError(
                f"Tentativo di query service-role su '{table}' senza user_id: "
                "senza RLS questo esporrebbe i dati di tutti i tenant."
            )
        self._client = client
        self._table = table
        self._user_id = str(user_id)
        self._column = _scope_column(table)

    @property
    def _builder(self) -> AsyncRequestBuilder:
        return self._client.table(self._table)

    def select(
        self, *columns: str, count: CountMethod | None = None
    ) -> AsyncSelectRequestBuilder:
        return self._builder.select(*columns, count=count).eq(self._column, self._user_id)

    def insert(self, values: dict[str, Any]) -> AsyncQueryRequestBuilder:
        return self._builder.insert(self._with_owner(values))

    def upsert(
        self, values: dict[str, Any], *, on_conflict: str = ""
    ) -> AsyncQueryRequestBuilder:
        # Il target del conflitto deve includere la colonna del proprietario:
        # un `on_conflict` senza `user_id` farebbe collidere l'upsert con la
        # riga di un altro tenant, che il service-role può sovrascrivere.
        if on_conflict:
            targets = {column.strip() for column in on_conflict.split(",")}
            if self._column not in targets:
                raise RuntimeError(
                    f"on_conflict='{on_conflict}' non include '{self._column}': "
                    "l'upsert potrebbe sovrascrivere la riga di un altro utente."
                )
        return self._builder.upsert(self._with_owner(values), on_conflict=on_conflict)

    def update(self, values: dict[str, Any]) -> AsyncFilterRequestBuilder:
        # Il proprietario non è modificabile: riassegnare una riga a un altro
        # utente non è un'operazione che questa API deve poter esprimere.
        payload = {k: v for k, v in values.items() if k != self._column}
        return self._builder.update(payload).eq(self._column, self._user_id)

    def delete(self) -> AsyncFilterRequestBuilder:
        return self._builder.delete().eq(self._column, self._user_id)

    def _with_owner(self, values: dict[str, Any]) -> dict[str, Any]:
        """Forza il proprietario nel payload, ignorando quanto eventualmente passato."""
        return {**values, self._column: self._user_id}


async def service_table(table: str, user_id: str) -> ScopedServiceTable:
    """Unico accesso previsto alle tabelle col client service-role."""
    return ScopedServiceTable(await _get_service_client(), table, user_id)


async def unscoped_service_table(table: str, *, reason: str) -> AsyncRequestBuilder:
    """Accesso service-role **non** filtrato per utente.

    Ammesso solo dove uno scoping per utente non è definibile: il job cron deve
    enumerare i creator attivi di tutti i tenant, perché non esiste un utente
    "corrente" da cui derivare il filtro. Da lì in poi ogni operazione riparte
    dallo `user_id` letto sulla riga del creator, tramite `service_table`.

    Vincolata da `_UNSCOPED_ALLOWED`: il log del motivo aiuta la review, ma non
    impedisce nulla da solo. L'allowlist sì — estenderla è una modifica visibile
    a questo modulo, non una riga in più in un router.
    """
    if table not in _UNSCOPED_ALLOWED:
        raise RuntimeError(
            f"Query service-role non scopata vietata sulla tabella '{table}'. "
            "Usare service_table(table, user_id); se il bypass è davvero "
            "necessario, aggiungere la tabella a _UNSCOPED_ALLOWED motivandolo."
        )
    logger.info("Query service-role non scopata su '%s' — motivo: %s", table, reason)
    return (await _get_service_client()).table(table)


# =============================================================================
# Traduzione degli errori PostgREST
# =============================================================================


def translate_postgrest_error(exc: APIError, *, context: str) -> Exception:
    """Converte un errore PostgREST in un'eccezione di dominio.

    Il messaggio del database resta nei log: può contenere nomi di constraint,
    frammenti di query e valori delle colonne, che non vanno esposti al client.
    """
    code = getattr(exc, "code", None)

    if code == _UNIQUE_VIOLATION:
        logger.info("Violazione di unicità (%s): %s", context, exc.message)
        return ConflictError("La risorsa esiste già.")

    if code == _FOREIGN_KEY_VIOLATION:
        logger.info("Violazione di foreign key (%s): %s", context, exc.message)
        return ConflictError("Riferimento a una risorsa inesistente.")

    logger.error(
        "Errore Supabase (%s) code=%s message=%s", context, code, exc.message, exc_info=True
    )
    return DatabaseError()


@asynccontextmanager
async def db_errors(context: str) -> AsyncIterator[None]:
    """Traduce gli errori PostgREST sollevati nel blocco."""
    try:
        yield
    except APIError as exc:
        raise translate_postgrest_error(exc, context=context) from exc
