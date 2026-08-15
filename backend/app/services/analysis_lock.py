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

from app.core.config import Settings
from app.schemas.analysis import AnalysisMode
from app.services.expiring_lock import acquisisci, rilascia
from app.services.supabase_service import service_table

logger = logging.getLogger(__name__)

_TABLE = "analysis_locks"


def _chiave(cache_key: str, mode: AnalysisMode) -> dict[str, str]:
    """Le colonne che identificano il lock di un'analisi."""
    return {"cache_key": cache_key, "analysis_mode": mode}


def _descrizione(cache_key: str, mode: AnalysisMode) -> str:
    """Come il lock compare nei log."""
    return f"analisi di {cache_key} (modalità {mode})"


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
    return await acquisisci(
        await service_table(_TABLE, user_id),
        _chiave(cache_key, mode),
        settings.analysis_lock_ttl_seconds,
        descrizione=_descrizione(cache_key, mode),
        contesto="analisi",
    )


async def release(user_id: str, cache_key: str, mode: AnalysisMode) -> None:
    """Rilascia il lock. Non solleva mai — vedi `expiring_lock.rilascia`."""
    await rilascia(
        lambda: service_table(_TABLE, user_id),
        _chiave(cache_key, mode),
        descrizione=_descrizione(cache_key, mode),
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
