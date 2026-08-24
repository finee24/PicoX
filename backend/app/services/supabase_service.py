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
from app.core.exceptions import (
    AnalysisQuotaError,
    ConflictError,
    DatabaseError,
    GlobalCapacityError,
    PlanLimitError,
    ValidationQuotaError,
)

logger = logging.getLogger(__name__)

# Codici Postgres che hanno una traduzione HTTP precisa.
_UNIQUE_VIOLATION: Final = "23505"
_FOREIGN_KEY_VIOLATION: Final = "23503"
# SQLSTATE di dominio, sollevato da `enforce_creator_limit` (migration 0004).
# Non è uno standard Postgres: è uno spazio riservato a noi, così un limite di
# piano non si confonde con un guasto del database.
_PLAN_LIMIT: Final = "PX001"
# Sollevato da `enforce_analysis_quota` (migration 0008). Distinto da PX001
# di proposito: sono due limiti di piano diversi, e il client deve poterli
# distinguere per dire la cosa giusta all'utente.
_ANALYSIS_QUOTA: Final = "PX002"
# Sollevato da `enforce_validation_quota` (migration 0010). Terzo codice per lo
# stesso motivo per cui esiste il secondo: «hai troppi creator», «hai esaurito
# le analisi» e «hai esaurito le verifiche» sono tre istruzioni diverse.
_VALIDATION_QUOTA: Final = "PX003"
# Sollevato da `enforce_global_spend_cap` (migration 0011). Quarto codice, ma di
# natura diversa dai tre sopra: quelli sono limiti del singolo utente e vanno
# spiegati; questo è una rete di sicurezza sull'intero servizio e verso il
# client resta **generico** di proposito. Il codice separato serve qui dentro,
# perché il backend deve poterlo registrare come incidente invece che come
# normale rifiuto di quota — non serve al client, che non deve saperlo.
_GLOBAL_SPEND_CAP: Final = "PX004"

# Campi che `enforce_global_spend_cap` mette nel `detail` dell'eccezione, nella
# forma `chiave=valore;chiave=valore`. Si fermano nei log: sono esattamente i
# numeri che direbbero a chi sonda il sistema quanto manca al blocco.
_CAMPI_SPESA: Final[tuple[str, ...]] = (
    "spend_today_usd",
    "row_cost_usd",
    "daily_cap_usd",
    "source_table",
)

# `profiles` è scopata sulla PK, non su una colonna `user_id`.
_SCOPE_COLUMN: Final[dict[str, str]] = {
    "profiles": "id",
    "creators": "user_id",
    "insights": "user_id",
    "analysis_locks": "user_id",
    "analysis_events": "user_id",
    "validation_events": "user_id",
}

# Tabelle su cui è ammessa una query service-role non filtrata per utente.
# `creators` c'è perché il cron deve enumerare i creator attivi di tutti i
# tenant e non ha un utente corrente da cui derivare il filtro. `job_locks` c'è
# perché è coordinamento fra processi: non contiene dati di alcun utente e non
# ha una colonna su cui scopare. `creator_validations` c'è per la stessa ragione
# di `job_locks` e non per comodità: è una cache di dati **già pubblici** sul
# profilo di un creator, la sua chiave è `(platform, identifier)` e un utente
# non compare da nessuna parte. Renderla per utente moltiplicherebbe la spesa
# esterna per il numero di utenti interessati allo stesso creator, cioè
# esattamente ciò che la cache esiste per evitare (migration 0010). Chi ha
# validato cosa sta invece in `validation_events`, che ha `user_id` ed è scopata
# come le altre. `insights` non c'è, e non deve entrarci: lì una query non
# scopata è una fuga di dati.
_UNSCOPED_ALLOWED: Final[frozenset[str]] = frozenset(
    {"creators", "job_locks", "creator_validations"}
)

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
# Query obbligatoriamente scopate
# =============================================================================


class ScopedTable:
    """Builder che applica da sé il filtro sul proprietario a ogni operazione.

    Non è una comodità: è il punto in cui la regola "ogni query filtra su
    `user_id`" smette di dipendere dalla memoria di chi scrive il codice. Non
    espone un accesso grezzo alla tabella.

    Vale per **entrambi** i client, ed è il motivo per cui la classe non si
    chiama più `ScopedServiceTable`. Ciò che cambia fra i due è solo la
    conseguenza di un filtro dimenticato:

    * col service-role (`service_table`) il RLS è scavalcato, e la query
      restituirebbe le righe di tutti i tenant;
    * col client dell'utente (`scoped_table`) il RLS regge e la fuga non
      avviene — ma la query tornerebbe **vuota senza dirlo**, e un `404` al
      posto di un risultato è un bug che si insegue a lungo.

    Nessuna delle due è accettabile, e nessuna delle due va lasciata
    all'attenzione di chi scrive la route.
    """

    __slots__ = ("_client", "_column", "_table", "_user_id")

    def __init__(self, client: AsyncClient, table: str, user_id: str) -> None:
        if not user_id:
            raise RuntimeError(
                f"Tentativo di query su '{table}' senza user_id: col client "
                "service-role esporrebbe i dati di tutti i tenant, con quello "
                "dell'utente restituirebbe zero righe senza segnalarlo."
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


async def service_table(table: str, user_id: str) -> ScopedTable:
    """Unico accesso previsto alle tabelle col client service-role."""
    return ScopedTable(await _get_service_client(), table, user_id)


@asynccontextmanager
async def scoped_table(
    table: str,
    user_id: str,
    access_token: str,
    settings: Settings | None = None,
) -> AsyncIterator[ScopedTable]:
    """Come `service_table`, ma col client dell'utente e il RLS attivo.

    È un context manager e non una semplice `async def` perché il client
    scoped **va chiuso**: è costruito per richiesta (l'`Authorization` fa parte
    della sua configurazione) e non condiviso come quello service-role.

    Esiste per togliere l'ultima asimmetria del modulo. Fino a qui il filtro sul
    proprietario era strutturale solo col service-role, mentre le route che
    usano il JWT lo scrivevano a mano — `.eq("user_id", user.id)` ripetuto in
    quattro punti, con il RLS come unica rete se qualcuno lo dimenticava. Il RLS
    quella rete la fa davvero, ma il risultato di una dimenticanza è una lista
    vuota o un 404 inspiegabile, non un errore: il tipo di difetto che si scopre
    da un utente che segnala «non vedo più i miei dati».
    """
    async with scoped_client(access_token, settings) as client:
        yield ScopedTable(client, table, user_id)


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


def _dettagli_spesa(details: str | None) -> dict[str, str]:
    """Estrae i campi strutturati dal `detail` di `PX004`.

    Tollerante di proposito. Se il formato cambia, o un campo manca, si registra
    comunque il WARNING con quello che c'è: un log d'allarme che sparisce per un
    errore di parsing sarebbe molto peggio di un log incompleto, ed è l'unico
    posto in cui la spesa stimata del giorno viene scritta da qualche parte.
    """
    if not details:
        return {}

    estratti: dict[str, str] = {}
    for pezzo in details.split(";"):
        chiave, separatore, valore = pezzo.partition("=")
        if separatore and chiave.strip() in _CAMPI_SPESA:
            estratti[chiave.strip()] = valore.strip()
    return estratti


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

    if code == _ANALYSIS_QUOTA:
        logger.info("Quota giornaliera di analisi esaurita (%s): %s", context, exc.message)
        return AnalysisQuotaError()

    if code == _VALIDATION_QUOTA:
        logger.info("Quota giornaliera di validazioni esaurita (%s): %s", context, exc.message)
        return ValidationQuotaError()

    if code == _GLOBAL_SPEND_CAP:
        dettagli = _dettagli_spesa(getattr(exc, "details", None))
        # WARNING e non INFO come le tre quote qui sopra, che sono il sistema
        # che funziona come previsto. Questa è la rete di sicurezza che
        # interviene: quando compare, qualcosa è già andato storto — un abuso
        # distribuito, un difetto che riaccoda lavoro, un picco non previsto — e
        # il servizio è appena diventato indisponibile per tutti, non per chi ha
        # fatto la richiesta.
        #
        # È anche l'unico punto del sistema in cui la spesa stimata del giorno
        # viene registrata: il dato che la voce B4 dell'audit chiede e che oggi
        # non esiste altrove. I campi passano da `extra=` perché
        # `JsonLogFormatter` promuove a chiave di primo livello tutto ciò che
        # non è un attributo standard di `LogRecord` — quindi diventano
        # interrogabili senza dover rileggere il testo del messaggio.
        logger.warning(
            "Tetto di spesa globale raggiunto (%s): stimati %s USD su un tetto di %s USD",
            context,
            dettagli.get("spend_today_usd", "?"),
            dettagli.get("daily_cap_usd", "?"),
            extra={"evento": "tetto_spesa_globale", **dettagli},
        )
        return GlobalCapacityError()

    if code == _PLAN_LIMIT:
        # Senza questo ramo il limite di piano finirebbe nel caso generico qui
        # sotto, e l'utente leggerebbe "servizio dati non disponibile" avendo
        # semplicemente raggiunto il tetto del proprio piano: un 503 al posto di
        # un 409, e un messaggio che manda a cercare un guasto inesistente.
        logger.info("Limite di piano raggiunto (%s): %s", context, exc.message)
        return PlanLimitError()

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
