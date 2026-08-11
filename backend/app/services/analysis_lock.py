"""Lock a scadenza sulle analisi in corso.

Impedisce che due richieste concorrenti sullo stesso `(utente, video,
modalità)` paghino entrambe Apify e Gemini.

**La chiave è `cache_key`, non l'URL** (migration `0009`): due forme dello
stesso video — su TikTok basta un username diverso nel path — hanno `video_url`
diversi ed è corretto che li abbiano, perché è il valore mostrato all'utente.
Se il lock arbitrasse su quello, quelle due richieste otterrebbero lock
distinti e pagherebbero entrambe: il difetto che questo modulo esiste per
chiudere, riaperto dalla porta di servizio. Lock e deduplica devono guardare
la stessa stringa. Il vincolo `UNIQUE (user_id,
cache_key)` su `insights` deduplica la **riga**, non il **lavoro**: fra la
lettura della cache e la scrittura del risultato passa l'intera pipeline, e in
quella finestra nulla fermava una seconda richiesta.

Perché una tabella e non `pg_advisory_lock`: questo backend non ha una
connessione Postgres da tenere aperta — parla col database solo via PostgREST,
in HTTP. Un lock di sessione richiederebbe di trattenere la sessione per tutta
l'analisi, e su una connessione poolata resterebbe orfano. Il ragionamento per
esteso sta in `supabase/migrations/0003_analysis_locks.sql`.

**Il lock scade da solo.** Non dipende dal fatto che qualcuno esegua il
rilascio: un processo ucciso da OOM o un container riavviato a metà deploy non
eseguono alcun `finally`. Ogni riga porta `expires_at`, e l'acquisizione
sottrae i lock scaduti con un `UPDATE` condizionale — un'operazione atomica,
valida anche fra istanze diverse, perché l'arbitro è il database e non il
processo.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.schemas.analysis import AnalysisMode
from app.services.supabase_service import db_errors, service_table

logger = logging.getLogger(__name__)

_TABLE = "analysis_locks"


async def acquire(
    user_id: str,
    cache_key: str,
    mode: AnalysisMode,
    settings: Settings,
) -> bool:
    """Prova a prendere il lock. `True` se lo si è ottenuto.

    Due passi, entrambi atomici lato database:

    1. `INSERT`, che riesce solo se per quella chiave non esiste alcuna riga;
    2. se l'insert collide, `UPDATE ... WHERE expires_at <= now()`, che riesce
       solo se il lock esistente è **scaduto** e in tal caso lo sottrae.

    Il secondo passo è ciò che rende il meccanismo immune ai lock orfani: non
    esiste stato che un crash possa lasciare bloccato per sempre.
    """
    adesso = datetime.now(UTC)
    scadenza = adesso + timedelta(seconds=settings.analysis_lock_ttl_seconds)

    locks = await service_table(_TABLE, user_id)

    try:
        async with db_errors("acquisizione lock analisi"):
            await locks.insert(
                {
                    "cache_key": cache_key,
                    "analysis_mode": mode,
                    "locked_at": adesso.isoformat(),
                    "expires_at": scadenza.isoformat(),
                }
            ).execute()
        return True
    except ConflictError:
        # Un lock per questa chiave esiste già. Resta da capire se è vivo o
        # abbandonato, e la risposta deve arrivare dal database in un'unica
        # operazione: leggerlo e poi decidere lascerebbe spazio a due
        # richieste che lo sottraggono entrambe.
        pass

    async with db_errors("sottrazione lock analisi scaduto"):
        risultato = await (
            locks.update(
                {
                    "locked_at": adesso.isoformat(),
                    "expires_at": scadenza.isoformat(),
                }
            )
            .eq("cache_key", cache_key)
            .eq("analysis_mode", mode)
            .lte("expires_at", adesso.isoformat())
            .execute()
        )

    sottratto = bool(risultato.data)
    if sottratto:
        logger.warning(
            "Lock analisi scaduto e sottratto per %s (modalità %s): "
            "il detentore precedente non lo ha rilasciato.",
            cache_key,
            mode,
        )
    return sottratto


async def release(user_id: str, cache_key: str, mode: AnalysisMode) -> None:
    """Rilascia il lock. Non solleva mai.

    È un'ottimizzazione del caso normale, non la garanzia: libera subito la
    chiave invece di far attendere la scadenza. Se fallisce — o se non viene
    mai eseguito, come in un crash — il lock scade comunque da sé, quindi qui
    un errore non merita di trasformare un'analisi riuscita in un 500.
    """
    try:
        locks = await service_table(_TABLE, user_id)
        await (
            locks.delete()
            .eq("cache_key", cache_key)
            .eq("analysis_mode", mode)
            .execute()
        )
    except Exception:  # noqa: BLE001 — vedi docstring: qui nulla deve propagarsi
        logger.warning(
            "Rilascio del lock analisi fallito per %s (modalità %s): "
            "scadrà da sé entro il TTL.",
            cache_key,
            mode,
            exc_info=False,
        )


@asynccontextmanager
async def analysis_lock(
    user_id: str,
    cache_key: str,
    mode: AnalysisMode,
    settings: Settings,
) -> AsyncIterator[bool]:
    """Contesto che espone se il lock è stato ottenuto.

    Il rilascio avviene solo se lo si era ottenuto: rilasciare un lock altrui
    aprirebbe la finestra che questo modulo esiste per chiudere.
    """
    ottenuto = await acquire(user_id, cache_key, mode, settings)
    try:
        yield ottenuto
    finally:
        if ottenuto:
            await release(user_id, cache_key, mode)
