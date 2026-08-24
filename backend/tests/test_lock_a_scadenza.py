"""Il meccanismo condiviso dei lock a scadenza distingue un lock dall'altro.

`analysis_lock` e `job_lock` erano gemelli con ~90 righe identiche e sono stati
ricondotti a un solo meccanismo (`expiring_lock`). Unificare due copie ha però
un costo: da qui in avanti un difetto nel codice condiviso li colpisce
**entrambi**, e in silenzio.

Questo file copre la parte che nessun test toccava. Rimuovendo il filtro sulle
colonne che identificano il lock — `_con_chiave` — l'intera suite restava verde,
perché nei suoi scenari esiste sempre **un solo lock per volta**: senza una
seconda riga con cui confondersi, un filtro mancante non ha modo di manifestarsi.

Le conseguenze reali sono due, e sono opposte fra loro:

* rilasciando un lock si rilascerebbero **tutti** quelli dello stesso utente (o
  tutti i job), riaprendo la finestra di doppia spesa che la migration `0003`
  esiste per chiudere;
* acquisendone uno si sposterebbe la scadenza di tutti gli altri, tenendo in vita
  lock che avrebbero dovuto essere sottratti.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.services import analysis_lock as lock_service
from app.services import job_lock
from tests.conftest import USER_ID
from tests.fake_supabase import FakeStore

ALTRA_CHIAVE = "cache-key-di-un-altro-video"
CHIAVE = "cache-key-del-video-in-analisi"


def _lock_vivo(cache_key: str, mode: str = "BOTH") -> dict[str, Any]:
    adesso = datetime.now(UTC)
    return {
        "user_id": USER_ID,
        "cache_key": cache_key,
        "analysis_mode": mode,
        "locked_at": adesso.isoformat(),
        "expires_at": (adesso + timedelta(minutes=10)).isoformat(),
    }


async def test_rilasciare_un_lock_non_tocca_gli_altri_dello_stesso_utente(
    store: FakeStore,
) -> None:
    """Due analisi in corso per lo stesso utente: chiuderne una non libera l'altra."""
    store.seed("analysis_locks", _lock_vivo(CHIAVE))
    store.seed("analysis_locks", _lock_vivo(ALTRA_CHIAVE))

    await lock_service.release(USER_ID, CHIAVE, "BOTH")

    superstiti = [riga["cache_key"] for riga in store.rows("analysis_locks")]
    assert superstiti == [ALTRA_CHIAVE]


async def test_rilasciare_un_lock_non_tocca_le_altre_modalita(
    store: FakeStore,
) -> None:
    """La modalità fa parte dell'identità del lock, non è un dettaglio del payload.

    Stesso video, due modalità: sono due analisi distinte e due spese distinte.
    """
    store.seed("analysis_locks", _lock_vivo(CHIAVE, "BOTH"))
    store.seed("analysis_locks", _lock_vivo(CHIAVE, "INFO"))

    await lock_service.release(USER_ID, CHIAVE, "BOTH")

    superstiti = [riga["analysis_mode"] for riga in store.rows("analysis_locks")]
    assert superstiti == ["INFO"]


async def test_acquisire_un_lock_non_prolunga_la_scadenza_degli_altri(
    store: FakeStore,
) -> None:
    """Un lock scaduto altrove non deve tornare vivo perché ne nasce un altro."""
    vecchio = datetime.now(UTC) - timedelta(minutes=5)
    store.seed(
        "analysis_locks",
        {
            "user_id": USER_ID,
            "cache_key": ALTRA_CHIAVE,
            "analysis_mode": "BOTH",
            "locked_at": (vecchio - timedelta(minutes=30)).isoformat(),
            "expires_at": vecchio.isoformat(),
        },
    )
    store.seed("analysis_locks", _lock_vivo(CHIAVE))

    # Il lock su CHIAVE è vivo: l'acquisizione fallisce e passa dal ramo di
    # sottrazione, che è proprio quello che filtra sulle colonne chiave.
    assert not await lock_service.acquire(USER_ID, CHIAVE, "BOTH", get_settings())

    per_chiave = {
        riga["cache_key"]: riga["expires_at"] for riga in store.rows("analysis_locks")
    }
    assert per_chiave[ALTRA_CHIAVE] == vecchio.isoformat(), (
        "la scadenza di un lock estraneo è stata spostata: il filtro sulla "
        "chiave non è stato applicato"
    )


async def test_rilasciare_un_job_non_tocca_gli_altri_job(store: FakeStore) -> None:
    """La stessa garanzia sul gemello, che ora condivide il meccanismo."""
    adesso = datetime.now(UTC)
    for nome in ("cron:check-updates", "cron:potatura"):
        store.seed(
            "job_locks",
            {
                "job_name": nome,
                "locked_at": adesso.isoformat(),
                "expires_at": (adesso + timedelta(minutes=10)).isoformat(),
            },
        )

    await job_lock.release("cron:check-updates")

    superstiti = [riga["job_name"] for riga in store.rows("job_locks")]
    assert superstiti == ["cron:potatura"]
