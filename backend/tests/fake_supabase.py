"""Doppio di test di Supabase: store in memoria + builder in stile PostgREST.

Perché un fake e non dei `Mock`. Con dei mock si verifica che il codice *abbia
chiamato* `.eq("user_id", ...)`, cioè si riscrive l'implementazione dentro il
test: il giorno in cui la query cambia forma il test fallisce pur restando il
comportamento corretto. Qui invece si verifica il *risultato* — quali righe
tornano, quali finiscono nello store — che è ciò che conta davvero.

**Il fake applica il RLS.** Il client costruito con la anon key più un JWT filtra
da sé le righe sul `sub` del token, esattamente come farebbero le policy della
migration 0001; quello costruito con la service role key non filtra nulla, come
il ruolo `service_role` che ha `BYPASSRLS`. È la parte che dà valore al doppio:
se un giorno una query scoped perdesse il filtro sul tenant, il test vedrebbe le
righe di un altro utente — cioè lo stesso fallimento che si avrebbe in
produzione, invece di un verde ingannevole.

Copre solo il sottoinsieme di PostgREST realmente usato dall'applicazione. Un
operatore non implementato solleva `NotImplementedError` invece di essere
ignorato in silenzio: un filtro che sparisce senza far rumore è esattamente il
modo in cui un test finisce per non provare più nulla.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from postgrest.exceptions import APIError

Row = dict[str, Any]
Predicate = Callable[[Row], bool]

# Colonna che identifica il proprietario, per tabella. Rispecchia
# `_SCOPE_COLUMN` di `app.services.supabase_service` e le policy RLS.
_OWNER_COLUMN: dict[str, str] = {
    "profiles": "id",
    "creators": "user_id",
    "insights": "user_id",
    "analysis_locks": "user_id",
    "analysis_events": "user_id",
    "validation_events": "user_id",
    # `creator_validations` **non** compare qui, e l'assenza è la sua
    # definizione: è una cache condivisa senza proprietario (migration 0010).
    # Elencarla con una colonna inventata farebbe passare i test su un modello
    # di accesso che in produzione non esiste.
}

# Colonne nullable che il database restituisce comunque, anche quando l'INSERT
# non le nomina. Servono al doppio per non produrre righe *senza* la chiave:
# `insights.creator_id` viene omesso di proposito dall'upsert — è così che si
# evita di cancellare l'attribuzione al creator — e senza questa lista la riga
# nuova ne resterebbe priva, cosa che in Postgres non può accadere.
_NULLABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "insights": (
        "creator_id",
        "thumbnail_url",
        "summary_data",
        "style_data",
        "inverse_script_template",
    ),
}


def _as_text(value: Any) -> str:
    """PostgREST confronta i filtri come testo (gli UUID arrivano da URL)."""
    return "" if value is None else str(value)


def _as_datetime(value: Any) -> datetime | None:
    """Il valore come `datetime`, se lo è o se ne ha la forma. Altrimenti `None`."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


class FakeAPIError(APIError):
    """Errore del database nella forma che l'applicazione sa tradurre.

    Sottoclasse dell'`APIError` vero e non di `Exception`: solo così passa da
    `db_errors` -> `translate_postgrest_error`, cioè dalla mappatura reale
    "23505 -> 409 Conflict". Con un'eccezione qualsiasi il test verificherebbe
    un percorso d'errore che in produzione non esiste.
    """

    def __init__(self, code: str, message: str, details: str = "") -> None:
        # `details` è il campo in cui `enforce_global_spend_cap` (migration
        # 0011) mette i numeri della spesa: il messaggio resta generico e i
        # numeri viaggiano di lato, perché il primo può finire sotto gli occhi
        # di qualcuno e i secondi no. Ha un default vuoto perché nessuno degli
        # altri SQLSTATE lo usa.
        super().__init__({"message": message, "code": code, "hint": "", "details": details})


class FakeResult:
    """Ciò che l'applicazione legge da `execute()`: `.data` e `.count`."""

    def __init__(self, data: list[Row], count: int | None = None) -> None:
        self.data = data
        self.count = count


# =============================================================================
# Parsing dei filtri `or=(...)`
# =============================================================================


def _field_getter(column: str) -> Callable[[Row], Any]:
    """Supporta sia `colonna` sia l'accesso jsonb `colonna->>campo`."""
    if "->>" in column:
        base, _, field = column.partition("->>")

        def read_json(row: Row) -> Any:
            payload = row.get(base)
            return payload.get(field) if isinstance(payload, dict) else None

        return read_json
    return lambda row: row.get(column)


def _parse_or_clause(clause: str) -> Predicate:
    column, operator, value = clause.split(".", 2)
    read = _field_getter(column)

    if operator == "cs":  # contains: array che include il valore
        wanted = value.strip("{}")
        return lambda row: isinstance(read(row), list) and wanted in read(row)

    if operator == "ilike":
        needle = value.strip("*").lower()

        def matches(row: Row) -> bool:
            actual = read(row)
            return isinstance(actual, str) and needle in actual.lower()

        return matches

    raise NotImplementedError(f"Operatore PostgREST non supportato nel fake: {operator}")


# =============================================================================
# Query builder
# =============================================================================


class FakeQuery:
    """Builder incatenabile che imita `postgrest.AsyncRequestBuilder`."""

    def __init__(self, store: FakeStore, table: str, owner_id: str | None) -> None:
        self._store = store
        self._table = table
        # `None` = service role (nessun RLS). Altrimenti è il `sub` del JWT.
        self._owner_id = owner_id

        self._operation = "select"
        self._payload: Row | None = None
        self._on_conflict = ""
        self._predicates: list[Predicate] = []
        self._want_count = False
        self._order_column: str | None = None
        self._order_desc = False
        self._range: tuple[int, int] | None = None
        self._limit: int | None = None
        self._negate_next = False

    # --- costruzione dei filtri ---------------------------------------------

    def _add(self, predicate: Predicate) -> FakeQuery:
        if self._negate_next:
            self._negate_next = False
            self._predicates.append(lambda row: not predicate(row))
        else:
            self._predicates.append(predicate)
        return self

    @property
    def not_(self) -> FakeQuery:
        self._negate_next = True
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:
        target = _as_text(value)
        return self._add(lambda row: _as_text(row.get(column)) == target)

    def in_(self, column: str, values: Iterable[Any]) -> FakeQuery:
        wanted = {_as_text(value) for value in values}
        return self._add(lambda row: _as_text(row.get(column)) in wanted)

    def lte(self, column: str, value: Any) -> FakeQuery:
        """`<=` sul valore della colonna, con i timestamp confrontati come tali.

        Il confronto testuale funzionerebbe sugli ISO-8601 solo finché formato e
        fuso restano identici fra chi scrive e chi filtra. È esattamente il tipo
        di assunzione che regge nei test e cade in produzione, quindi qui si
        confrontano `datetime` veri e si ricade sul testo solo per i valori che
        timestamp non sono.
        """
        limite = _as_datetime(value)

        def predicato(row: Row) -> bool:
            corrente = _as_datetime(row.get(column))
            if limite is not None and corrente is not None:
                return corrente <= limite
            return _as_text(row.get(column)) <= _as_text(value)

        return self._add(predicato)

    def is_(self, column: str, value: Any) -> FakeQuery:
        if value in ("null", None):
            return self._add(lambda row: row.get(column) is None)
        raise NotImplementedError(f"is_({value!r}) non supportato nel fake")

    def or_(self, expression: str) -> FakeQuery:
        clauses = [part for part in expression.split(",") if part]
        predicates = [_parse_or_clause(clause) for clause in clauses]
        return self._add(lambda row: any(check(row) for check in predicates))

    # --- forma del risultato -------------------------------------------------

    def select(self, *_columns: str, count: Any = None) -> FakeQuery:
        # La proiezione delle colonne non è simulata: restituire la riga intera
        # è un superset di quanto chiesto, e nessun test dipende dall'assenza di
        # una colonna. La differenza che conta — quali *righe* tornano — è
        # invece riprodotta fedelmente.
        self._operation = "select"
        self._want_count = count is not None
        return self

    def order(self, column: str, desc: bool = False) -> FakeQuery:
        self._order_column = column
        self._order_desc = desc
        return self

    def range(self, start: int, end: int) -> FakeQuery:
        self._range = (start, end)  # estremi inclusi, come PostgREST
        return self

    def limit(self, size: int) -> FakeQuery:
        self._limit = size
        return self

    # --- scritture -----------------------------------------------------------

    def insert(self, values: Row) -> FakeQuery:
        self._operation = "insert"
        self._payload = dict(values)
        return self

    def upsert(self, values: Row, *, on_conflict: str = "") -> FakeQuery:
        self._operation = "upsert"
        self._payload = dict(values)
        self._on_conflict = on_conflict
        return self

    def update(self, values: Row) -> FakeQuery:
        self._operation = "update"
        self._payload = dict(values)
        return self

    def delete(self) -> FakeQuery:
        self._operation = "delete"
        return self

    # --- esecuzione ----------------------------------------------------------

    async def execute(self) -> FakeResult:
        rows = self._store.rows(self._table)

        match self._operation:
            case "select":
                return self._run_select(rows)
            case "insert":
                return self._run_insert(rows)
            case "upsert":
                return self._run_upsert(rows)
            case "update":
                return self._run_update(rows)
            case "delete":
                return self._run_delete(rows)
            case _:  # pragma: no cover — difensivo
                raise NotImplementedError(self._operation)

    # Il RLS del fake: la stessa riga che una policy `auth.uid() = user_id`
    # renderebbe invisibile qui non entra mai nel result set.
    def _visible(self, rows: list[Row]) -> list[Row]:
        if self._owner_id is None:
            return rows
        column = _OWNER_COLUMN.get(self._table)
        if column is None:
            return rows
        return [row for row in rows if _as_text(row.get(column)) == self._owner_id]

    def _matching(self, rows: list[Row]) -> list[Row]:
        candidates = self._visible(rows)
        return [row for row in candidates if all(check(row) for check in self._predicates)]

    def _run_select(self, rows: list[Row]) -> FakeResult:
        found = self._matching(rows)

        if (order_column := self._order_column) is not None:
            found.sort(
                key=lambda row: _as_text(row.get(order_column)),
                reverse=self._order_desc,
            )

        # `count` è il totale *prima* della paginazione: è ciò che alimenta
        # `total` e quindi `has_more` nella risposta.
        total = len(found) if self._want_count else None

        if self._range is not None:
            start, end = self._range
            found = found[start : end + 1]
        if self._limit is not None:
            found = found[: self._limit]

        return FakeResult([dict(row) for row in found], total)

    def _new_row(self, values: Row) -> Row:
        now = datetime.now(UTC).isoformat()
        row: Row = {"id": str(uuid4()), "created_at": now, **values}
        if self._table == "creators":
            row.setdefault("updated_at", now)
            row.setdefault("is_active", True)
            row.setdefault("analysis_mode", "BOTH")
        # Una colonna esiste anche quando l'INSERT non la nomina: il database
        # la valorizza a NULL e ogni SELECT la restituisce. Ometterla qui
        # produrrebbe righe prive della chiave, che è uno stato che in
        # produzione non esiste — e farebbe fallire la validazione dei modelli
        # di risposta per un motivo inventato dal doppio.
        for colonna in _NULLABLE_COLUMNS.get(self._table, ()):
            row.setdefault(colonna, None)
        return row

    def _reject_if_rls_violation(self, values: Row) -> None:
        """Equivalente del `WITH CHECK` delle policy di INSERT/UPDATE."""
        if self._owner_id is None:
            return
        column = _OWNER_COLUMN.get(self._table)
        if column and column in values and _as_text(values[column]) != self._owner_id:
            raise FakeAPIError("42501", "new row violates row-level security policy")

    def _run_insert(self, rows: list[Row]) -> FakeResult:
        assert self._payload is not None
        self._reject_if_rls_violation(self._payload)
        self._store.enforce_unique(self._table, self._payload)
        row = self._new_row(self._payload)
        rows.append(row)
        return FakeResult([dict(row)])

    def _run_upsert(self, rows: list[Row]) -> FakeResult:
        assert self._payload is not None
        self._reject_if_rls_violation(self._payload)

        targets = [part.strip() for part in self._on_conflict.split(",") if part.strip()]
        if targets:
            for row in self._visible(rows):
                if all(
                    _as_text(row.get(key)) == _as_text(self._payload.get(key))
                    for key in targets
                ):
                    row.update(self._payload)
                    return FakeResult([dict(row)])

        self._store.enforce_unique(self._table, self._payload)
        row = self._new_row(self._payload)
        rows.append(row)
        return FakeResult([dict(row)])

    def _run_update(self, rows: list[Row]) -> FakeResult:
        assert self._payload is not None
        touched = self._matching(rows)
        for row in touched:
            row.update(self._payload)
            if self._table == "creators":
                row["updated_at"] = datetime.now(UTC).isoformat()
        return FakeResult([dict(row) for row in touched])

    def _run_delete(self, rows: list[Row]) -> FakeResult:
        removed = self._matching(rows)
        removed_ids = {row["id"] for row in removed}
        rows[:] = [row for row in rows if row["id"] not in removed_ids]

        # Riproduce `ON DELETE SET NULL` su `insights.creator_id`: gli insight
        # del creator rimosso restano, orfani ma intatti. È il comportamento che
        # il prodotto promette, quindi il doppio deve averlo.
        if self._table == "creators":
            for insight in self._store.rows("insights"):
                if _as_text(insight.get("creator_id")) in removed_ids:
                    insight["creator_id"] = None

        return FakeResult([dict(row) for row in removed])


# =============================================================================
# Store e client
# =============================================================================

# Vincoli UNIQUE della migration 0001.
_UNIQUE_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "creators": ("user_id", "username", "platform"),
    # Spostato dalla migration 0009: la deduplica vive su `cache_key`, non
    # sull'URL mostrato. Se il doppio tenesse il vincolo vecchio, i test sulla
    # concorrenza passerebbero per una ragione che in produzione non esiste.
    "insights": ("user_id", "cache_key"),
    # Primary key composita: è il vincolo su cui poggia il mutex, quindi il
    # doppio deve farla rispettare come il database, altrimenti i test sulla
    # concorrenza passerebbero per un motivo che in produzione non esiste.
    "analysis_locks": ("user_id", "cache_key", "analysis_mode"),
    # Stesso motivo, chiave singola: è `job_name` a garantire un giro di cron
    # per volta. Senza questo vincolo nel doppio, due esecuzioni sovrapposte
    # otterrebbero entrambe il lock e il test verde non direbbe nulla.
    "job_locks": ("job_name",),
    # Primary key della cache delle validazioni (migration 0010). Senza, un
    # upsert che non trova la riga esistente ne aggiungerebbe una seconda per
    # la stessa chiave, e il test sulla cache passerebbe leggendo la prima
    # mentre in produzione ce n'è sempre e solo una.
    "creator_validations": ("platform", "normalized_identifier"),
}


class FakeStore:
    """Le tabelle in memoria, condivise da tutti i client del test."""

    def __init__(self) -> None:
        self._tables: dict[str, list[Row]] = {
            "profiles": [],
            "creators": [],
            "insights": [],
        }

    def rows(self, table: str) -> list[Row]:
        return self._tables.setdefault(table, [])

    def enforce_unique(self, table: str, values: Row) -> None:
        keys = _UNIQUE_CONSTRAINTS.get(table)
        if not keys:
            return
        for row in self.rows(table):
            if all(_as_text(row.get(key)) == _as_text(values.get(key)) for key in keys):
                raise FakeAPIError(
                    "23505",
                    f'duplicate key value violates unique constraint "{table}_key"',
                )

    # --- helper per i test ---------------------------------------------------

    def seed(self, table: str, row: Row) -> Row:
        stored: Row = {
            "id": str(uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            **row,
        }
        self.rows(table).append(stored)
        return stored

    def count(self, table: str) -> int:
        return len(self.rows(table))


class _FakePostgrest:
    async def aclose(self) -> None:
        return None


class FakeSupabaseClient:
    """Sostituto di `supabase.AsyncClient`.

    Il ruolo non è dichiarato dal test: viene dedotto dalla chiave con cui il
    client è stato costruito, come accade davvero. Così il doppio non può
    "concedere" al codice più privilegi di quelli che avrebbe in produzione.
    """

    def __init__(self, store: FakeStore, owner_id: str | None) -> None:
        self._store = store
        self._owner_id = owner_id
        self.postgrest = _FakePostgrest()

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self._store, name, self._owner_id)


def _subject_from_bearer(header: str | None) -> str | None:
    if not header:
        return None
    token = re.sub(r"^Bearer\s+", "", header.strip(), flags=re.IGNORECASE)
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    subject = claims.get("sub")
    return str(subject) if subject else None


def make_acreate_client(store: FakeStore, service_role_key: str):
    """Sostituto di `supabase.acreate_client` da iniettare con monkeypatch.

    È il punto di innesto giusto perché è l'unico da cui passano *entrambi* i
    client dell'applicazione: quello scoped e quello service-role. Sostituendo
    qui, la logica di `ScopedServiceTable` — il filtro obbligatorio sul
    proprietario, il rifiuto di un `on_conflict` incompleto — resta quella vera
    e viene esercitata dai test invece di essere aggirata.
    """

    async def acreate_client(url: str, key: str, options: Any = None) -> FakeSupabaseClient:
        if key == service_role_key:
            return FakeSupabaseClient(store, owner_id=None)  # BYPASSRLS

        headers = getattr(options, "headers", None) or {}
        subject = _subject_from_bearer(headers.get("Authorization"))
        return FakeSupabaseClient(store, owner_id=subject)

    return acreate_client


def make_jwt(
    user_id: str,
    secret: str,
    *,
    audience: str = "authenticated",
    expires_in: int = 3600,
) -> str:
    """JWT HS256 nella forma emessa da Supabase GoTrue."""
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "aud": audience,
            "role": "authenticated",
            "email": f"{user_id}@example.test",
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
        },
        secret,
        algorithm="HS256",
    )
