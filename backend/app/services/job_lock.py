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

from app.services.expiring_lock import acquisisci, rilascia
from app.services.supabase_service import unscoped_service_table

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
    return await acquisisci(
        await _table(),
        {"job_name": job_name},
        ttl_seconds,
        descrizione=f"del job '{job_name}'",
        contesto="job",
        # Diagnostico e solo diagnostico: non partecipa ad alcuna decisione,
        # perché fidarsi di un valore scritto dal detentore precedente
        # reintrodurrebbe il problema che la scadenza risolve.
        extra={"holder": _holder()},
    )


async def release(job_name: str) -> None:
    """Rilascia il lock. Non solleva mai — vedi `expiring_lock.rilascia`."""
    await rilascia(_table, {"job_name": job_name}, descrizione=f"del job '{job_name}'")


# NOTA — qui **non** esiste un context manager `job_lock()`, e l'assenza è
# deliberata: è l'unico punto in cui questo modulo diverge dal gemello
# `analysis_lock`, che invece ne ha uno (usato da `analyze.py`).
#
# Un CM legherebbe il rilascio allo scope della route, mentre il lock del cron
# deve **sopravvivere alla richiesta**: `check_updates` risponde a fine
# censimento e passa il lock al `BackgroundTask` (`_esegui_e_rilascia`), che lo
# rilascia minuti dopo. Da qui l'acquire/release esplicito con `rilascia_qui`.
#
# Ne era stato scritto uno per simmetria; è rimasto senza chiamanti e i test non
# lo toccavano. Rimosso, perché un helper che nessuno può usare senza
# reintrodurre il difetto che il modulo chiude è peggio di nessun helper.
