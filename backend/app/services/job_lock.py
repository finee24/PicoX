"""Lock a scadenza per job pianificati, uno per nome di job.

Garantisce che un solo giro di un dato job sia in esecuzione alla volta, anche
fra istanze diverse: l'arbitro è il database, non il processo.

È il gemello di `analysis_lock`, con due differenze deliberate.

**La chiave è un nome di job.** Non `(utente, video, modalità)`: un job
pianificato non ha un utente. Chi aggiunge un secondo job passa il proprio nome
e ha finito — nessuna migration, nessun registro, nessun modulo nuovo.

**In contesa non si attende: si salta.** `analysis_lock` fa attendere chi arriva
secondo, perché dall'altra parte c'è un utente che aspetta un risultato. Qui no:
un cron che si accoda è peggio di uno che salta un giro, perché la coda cresce
mentre la finestra successiva arriva comunque. Chi non ottiene il lock registra
il fatto e torna indietro.

Il rilascio in `finally` è un'ottimizzazione del caso normale, non la garanzia.
La garanzia è `expires_at`: un processo ucciso durante un deploy non esegue
nulla in uscita, e il lock viene riassorbito dal primo giro che arriva dopo la
scadenza. Il ragionamento per esteso, TTL compreso, sta in
`supabase/migrations/0005_job_locks.sql`.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from app.core.exceptions import ConflictError
from app.services.supabase_service import db_errors, unscoped_service_table

logger = logging.getLogger(__name__)

_TABLE = "job_locks"

# Nome del job cron. Costante qui e non stringa sparsa: il lock e il rilascio
# devono riferirsi alla stessa chiave, e una battitura diversa fra i due punti
# produrrebbe un lock che non protegge nulla senza fallire mai.
CRON_CHECK_UPDATES = "cron:check-updates"


def _holder() -> str:
    """Chi sta prendendo il lock. Solo diagnostico."""
    return f"{socket.gethostname()}/{os.getpid()}"


async def _table():
    """Accesso service-role non scopato: `job_locks` non ha una colonna utente.

    È esattamente il caso per cui `unscoped_service_table` esiste — non c'è
    alcun utente corrente da cui derivare un filtro — e la tabella è
    nell'allowlist di `supabase_service`.
    """
    return await unscoped_service_table(
        _TABLE, reason=f"lock dei job pianificati (tabella {_TABLE})"
    )


async def acquire(job_name: str, ttl_seconds: int) -> bool:
    """Prova a prendere il lock. `True` se lo si è ottenuto.

    Due passi, entrambi atomici lato database:

    1. `INSERT`, che riesce solo se per quel nome non esiste alcuna riga;
    2. se l'insert collide, `UPDATE ... WHERE expires_at <= now()`, che riesce
       solo se il lock esistente è **scaduto** e in tal caso lo sottrae.

    Il secondo passo è ciò che rende il meccanismo immune ai lock orfani: non
    esiste stato che un crash possa lasciare bloccato per sempre. Leggere e poi
    decidere, invece, lascerebbe spazio a due giri che lo sottraggono entrambi.
    """
    adesso = datetime.now(UTC)
    scadenza = adesso + timedelta(seconds=ttl_seconds)
    locks = await _table()

    try:
        async with db_errors("acquisizione lock job"):
            await locks.insert(
                {
                    "job_name": job_name,
                    "locked_at": adesso.isoformat(),
                    "expires_at": scadenza.isoformat(),
                    "holder": _holder(),
                }
            ).execute()
        return True
    except ConflictError:
        # Un lock per questo job esiste già: resta da capire se è vivo o
        # abbandonato, e la risposta deve arrivare dal database in un'unica
        # operazione condizionale.
        pass

    async with db_errors("sottrazione lock job scaduto"):
        risultato = await (
            locks.update(
                {
                    "locked_at": adesso.isoformat(),
                    "expires_at": scadenza.isoformat(),
                    "holder": _holder(),
                }
            )
            .eq("job_name", job_name)
            .lte("expires_at", adesso.isoformat())
            .execute()
        )

    sottratto = bool(risultato.data)
    if sottratto:
        logger.warning(
            "Lock del job '%s' scaduto e sottratto: il detentore precedente non "
            "lo ha rilasciato — processo terminato a metà giro, o giro più lungo "
            "del TTL configurato.",
            job_name,
        )
    return sottratto


async def release(job_name: str) -> None:
    """Rilascia il lock. Non solleva mai.

    È un'ottimizzazione del caso normale: libera subito la chiave invece di far
    attendere la scadenza. Se fallisce — o se non viene mai eseguito, come in un
    crash — il lock scade comunque da sé, quindi qui un errore non merita di
    trasformare un giro riuscito in un 500.
    """
    try:
        locks = await _table()
        await locks.delete().eq("job_name", job_name).execute()
    except Exception:  # noqa: BLE001 — vedi docstring: qui nulla deve propagarsi
        logger.warning(
            "Rilascio del lock del job '%s' fallito: scadrà da sé entro il TTL.",
            job_name,
            exc_info=False,
        )


@asynccontextmanager
async def job_lock(job_name: str, ttl_seconds: int) -> AsyncIterator[bool]:
    """Contesto che espone se il lock è stato ottenuto.

    Il rilascio avviene **solo** se lo si era ottenuto: rilasciare il lock di un
    altro giro aprirebbe esattamente la finestra che questo modulo chiude.
    """
    ottenuto = await acquire(job_name, ttl_seconds)
    try:
        yield ottenuto
    finally:
        if ottenuto:
            await release(job_name)
