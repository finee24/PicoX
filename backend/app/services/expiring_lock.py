"""Il meccanismo dei lock a scadenza, condiviso da `analysis_lock` e `job_lock`.

I due moduli erano gemelli dichiarati — `job_lock` lo diceva nella propria
docstring — e condividevano ~90 righe identiche: stessa sequenza di due passi
atomici, stessa gestione del conflitto, stesso rilascio che non propaga mai.
Divergevano in tre parametri, non in un'idea.

## I due passi, e perché sono due

1. `INSERT`, che riesce solo se per quella chiave non esiste alcuna riga;
2. se l'insert collide, `UPDATE … WHERE expires_at <= now()`, che riesce **solo
   se il lock esistente è scaduto** e in tal caso lo sottrae.

Il secondo passo è ciò che rende il meccanismo immune ai lock orfani: non esiste
stato che un crash possa lasciare bloccato per sempre. Ed è un'unica operazione
condizionale, non una lettura seguita da una decisione: leggere e poi decidere
lascerebbe spazio a due richieste che sottraggono entrambe lo stesso lock.

## Cosa resta ai due moduli

Ciò che il meccanismo non può sapere: **quale tabella**, **quali colonne
identificano il lock**, e **da quale accessor** passa la tabella —
`service_table` per i lock di un utente, `unscoped_service_table` per quelli dei
job, che un utente non ce l'hanno. Per questo la tabella arriva già costruita:
scegliere l'accessor qui avrebbe richiesto un flag, cioè avrebbe rimesso dentro
la differenza che si voleva togliere.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.core.exceptions import ConflictError
from app.services.supabase_service import db_errors

logger = logging.getLogger(__name__)


class TabellaLock(Protocol):
    """Ciò che serve al meccanismo, e nulla di più.

    Un `Protocol` e non un tipo concreto perché i due accessor restituiscono
    classi diverse — `ScopedTable` e il builder grezzo di PostgREST — che qui si
    comportano allo stesso modo. I parametri sono posizionali (`/`) perché i due
    li chiamano con nomi diversi, e il nome non fa parte di ciò che ci serve.
    """

    def insert(self, values: dict[str, Any], /) -> Any: ...
    def update(self, values: dict[str, Any], /) -> Any: ...
    def delete(self) -> Any: ...


def _con_chiave(query: Any, chiave: dict[str, str]) -> Any:
    """Applica come filtro le colonne che identificano il lock."""
    for colonna, valore in chiave.items():
        query = query.eq(colonna, valore)
    return query


async def acquisisci(
    locks: TabellaLock,
    chiave: dict[str, str],
    ttl_seconds: int,
    *,
    descrizione: str,
    contesto: str,
    extra: dict[str, str] | None = None,
) -> bool:
    """Prova a prendere il lock. `True` se lo si è ottenuto.

    `descrizione` compare nei log ed è ciò che permette a chi legge di capire
    *quale* lock è stato sottratto; `contesto` finisce in `db_errors`, che lo usa
    per etichettare l'errore PostgREST. `extra` sono le colonne che un tipo di
    lock scrive e l'altro no — oggi solo `holder`, diagnostico.
    """
    adesso = datetime.now(UTC)
    scadenza = adesso + timedelta(seconds=ttl_seconds)
    istanti = {
        "locked_at": adesso.isoformat(),
        "expires_at": scadenza.isoformat(),
        **(extra or {}),
    }

    try:
        async with db_errors(f"acquisizione lock {contesto}"):
            await locks.insert({**chiave, **istanti}).execute()
        return True
    except ConflictError:
        # Un lock per questa chiave esiste già. Resta da capire se è vivo o
        # abbandonato, e la risposta deve arrivare dal database in un'unica
        # operazione: vedi la docstring del modulo.
        pass

    async with db_errors(f"sottrazione lock {contesto} scaduto"):
        risultato = await (
            _con_chiave(locks.update(istanti), chiave)
            .lte("expires_at", adesso.isoformat())
            .execute()
        )

    sottratto = bool(risultato.data)
    if sottratto:
        logger.warning(
            "Lock %s scaduto e sottratto: il detentore precedente non lo ha "
            "rilasciato — processo terminato a metà, o durata superiore al TTL "
            "configurato.",
            descrizione,
        )
    return sottratto


async def rilascia(
    locks_factory: Any,
    chiave: dict[str, str],
    *,
    descrizione: str,
) -> None:
    """Rilascia il lock. **Non solleva mai.**

    È un'ottimizzazione del caso normale, non la garanzia: libera subito la
    chiave invece di far attendere la scadenza. Se fallisce — o se non viene mai
    eseguito, come in un crash — il lock scade comunque da sé, quindi qui un
    errore non merita di trasformare un'operazione riuscita in un 500.

    Prende una *factory* e non la tabella già costruita perché anche ottenerla
    può fallire, e anche quel fallimento va assorbito qui invece di propagarsi
    al chiamante.
    """
    try:
        locks = await locks_factory()
        await _con_chiave(locks.delete(), chiave).execute()
    except Exception:  # noqa: BLE001 — vedi docstring: qui nulla deve propagarsi
        logger.warning(
            "Rilascio del lock %s fallito: scadrà da sé entro il TTL.",
            descrizione,
            exc_info=False,
        )
