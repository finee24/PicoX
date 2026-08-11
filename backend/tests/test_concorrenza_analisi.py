"""Concorrenza sulle analisi: spesa duplicata e perdita di `creator_id`.

Due difetti distinti che nascevano dallo stesso punto — nessuna coordinazione
fra richieste che scrivono sulla stessa riga di `insights` — e che vanno
verificati separatamente, perché le garanzie sono diverse:

1. **spesa**: `UNIQUE (user_id, cache_key)` deduplica la riga, non il lavoro.
   Prima della correzione, 3 POST concorrenti sullo stesso video producevano
   3 chiamate Gemini, 3 run Apify e 3 download per finire in 1 riga sola;
2. **integrità**: l'upsert riscriveva `creator_id` a `NULL`, perché il payload
   conteneva sempre la chiave. Il path manuale non ha un creator da passare, il
   cron sì: bastava una rianalisi manuale per cancellare l'attribuzione. Non
   serviva concorrenza — vedi il test sequenziale in fondo.

I numeri misurati prima della correzione sono citati in ogni test: servono a
rendere evidente che l'asserzione non è sempre stata vera.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.api.v1.analyze import perform_analysis
from app.core.config import get_settings
from app.core.exceptions import AnalysisInProgressError, GeminiError
from app.services import analysis_lock as lock_service
from tests.conftest import USER_ID, FakeApify, FakeGemini
from tests.fake_supabase import FakeStore

VIDEO_URL = "https://tiktok.com/@creator/video/123"
# La chiave con cui il lock arbitra dalla migration 0009: non e' l'URL.
CACHE_KEY = "tiktok.com:123"
CREATOR_ID = "11111111-2222-3333-4444-555555555555"


async def _analizza(**kwargs: Any):
    """I doppi non implementano i protocolli reali: il confine di tipo sta qui."""
    return await perform_analysis(**kwargs)


def _rallenta(gemini: FakeGemini, secondi: float = 0.15) -> None:
    """Gemini reale impiega decine di secondi.

    Senza attesa la prima richiesta finirebbe prima che la seconda arrivi al
    lookup, e il test passerebbe senza aver mai messo in contesa il lock: verde
    per assenza di concorrenza, non per correttezza.
    """
    originale = gemini.analyze_video

    async def lento(*args: Any, **kwargs: Any):
        await asyncio.sleep(secondi)
        return await originale(*args, **kwargs)

    gemini.analyze_video = lento  # type: ignore[method-assign]


def _poll_rapido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYSIS_LOCK_POLL_SECONDS", "0.02")
    get_settings.cache_clear()


# =============================================================================
# 1. Spesa duplicata
# =============================================================================


async def test_richieste_concorrenti_pagano_una_sola_analisi(
    app: Any,
    auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
    downloads: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prima della correzione: 3 Gemini, 3 Apify, 3 download, 1 riga."""
    _poll_rapido(monkeypatch)
    _rallenta(gemini)
    payload = {"video_url": VIDEO_URL, "analysis_mode": "BOTH"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        risposte = await asyncio.gather(
            *(
                c.post("/api/v1/analyze-video", headers=auth_headers, json=payload)
                for _ in range(3)
            )
        )

    # Una sola analisi pagata: è il punto dell'intero meccanismo.
    assert len(gemini.calls) == 1
    assert len(apify.resolve_calls) == 1
    assert len(downloads) == 1
    assert store.count("insights") == 1

    # Chi ha vinto il lock ha creato (201), gli altri hanno ricevuto lo stesso
    # record come cache hit (200). Nessuno riceve un errore.
    assert sorted(r.status_code for r in risposte) == [200, 200, 201]
    assert len({r.json()["id"] for r in risposte}) == 1


async def test_utenti_diversi_sullo_stesso_video_non_si_bloccano(
    app: Any,
    auth_headers: dict[str, str],
    other_auth_headers: dict[str, str],
    store: FakeStore,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La chiave del lock include `user_id`: la cache è per tenant, e lo è anche
    il diritto di analizzare. Bloccarli insieme sarebbe una regressione."""
    _poll_rapido(monkeypatch)
    _rallenta(gemini)
    payload = {"video_url": VIDEO_URL, "analysis_mode": "BOTH"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        prima, seconda = await asyncio.gather(
            c.post("/api/v1/analyze-video", headers=auth_headers, json=payload),
            c.post("/api/v1/analyze-video", headers=other_auth_headers, json=payload),
        )

    assert prima.status_code == 201
    assert seconda.status_code == 201
    assert len(gemini.calls) == 2
    assert store.count("insights") == 2


# =============================================================================
# 2. `creator_id` non deve mai retrocedere a NULL
# =============================================================================


async def test_creator_id_sopravvive_a_una_analisi_manuale_concorrente(
    app: Any,
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prima della correzione `creator_id` finiva a `None`.

    L'ordine è forzato, e nell'unico verso che conta: **il manuale parte per
    primo e vince il lock**. È il caso che una prima versione della correzione
    non copriva — nessuno sovrascriveva niente, semplicemente l'analisi la
    scriveva chi il creator non ce l'ha, e il cron riceveva quel risultato come
    cache hit senza mai attribuirlo. Verificandolo con `asyncio.gather` e basta,
    il test passava o falliva a seconda di quale coroutine lo scheduler
    toccava per prima.
    """
    _poll_rapido(monkeypatch)
    _rallenta(gemini)
    settings = get_settings()

    async def cron_in_ritardo():
        # Abbastanza da garantire che il manuale abbia già preso il lock.
        await asyncio.sleep(0.03)
        return await _analizza(
            user_id=USER_ID, video_url=VIDEO_URL, mode="BOTH",
            settings=settings, gemini=gemini, apify=apify, creator_id=CREATOR_ID,
        )

    manuale = _analizza(
        user_id=USER_ID, video_url=VIDEO_URL, mode="BOTH",
        settings=settings, gemini=gemini, apify=apify,
    )
    await asyncio.gather(manuale, cron_in_ritardo())

    assert store.count("insights") == 1
    assert store.rows("insights")[0]["creator_id"] == CREATOR_ID
    # E una sola analisi pagata anche qui.
    assert len(gemini.calls) == 1


async def test_creator_id_sopravvive_a_una_rianalisi_sequenziale(
    app: Any,
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
) -> None:
    """Il caso senza alcuna concorrenza, che è quello che rendeva il difetto
    quotidiano: il cron analizza in INFO, l'utente più tardi chiede BOTH. La
    riga non copre la modalità, si rianalizza, e prima della correzione la
    riscrittura cancellava il creator."""
    settings = get_settings()

    await _analizza(
        user_id=USER_ID, video_url=VIDEO_URL, mode="INFO",
        settings=settings, gemini=gemini, apify=apify, creator_id=CREATOR_ID,
    )
    assert store.rows("insights")[0]["creator_id"] == CREATOR_ID

    await _analizza(
        user_id=USER_ID, video_url=VIDEO_URL, mode="BOTH",
        settings=settings, gemini=gemini, apify=apify,
    )

    riga = store.rows("insights")[0]
    assert riga["creator_id"] == CREATOR_ID
    # La rianalisi è avvenuta davvero: il test non passa per un cache hit.
    assert riga["style_data"] is not None


async def test_il_cron_puo_valorizzare_creator_id_su_una_riga_che_non_ce_l_ha(
    app: Any,
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
) -> None:
    """Omettere la chiave non deve diventare "non si può più scrivere".

    Un insight nato da un link incollato a mano non ha creator; se lo stesso
    video viene poi ripreso dal cron, l'attribuzione deve potersi aggiungere.
    """
    settings = get_settings()

    await _analizza(
        user_id=USER_ID, video_url=VIDEO_URL, mode="INFO",
        settings=settings, gemini=gemini, apify=apify,
    )
    assert store.rows("insights")[0]["creator_id"] is None

    await _analizza(
        user_id=USER_ID, video_url=VIDEO_URL, mode="BOTH",
        settings=settings, gemini=gemini, apify=apify, creator_id=CREATOR_ID,
    )

    assert store.rows("insights")[0]["creator_id"] == CREATOR_ID


# =============================================================================
# 3. Il lock non deve poter restare bloccato per sempre
# =============================================================================


async def test_un_lock_scaduto_viene_sottratto(store: FakeStore) -> None:
    """Il caso del crash: chi deteneva il lock non ha eseguito alcun rilascio.

    Senza sottrazione, quel `(utente, video, modalità)` resterebbe inanalizzabile
    per sempre — ed è la ragione per cui il lock ha una scadenza invece di
    affidarsi al `finally`.
    """
    scaduto = datetime.now(UTC) - timedelta(minutes=5)
    store.seed(
        "analysis_locks",
        {
            "user_id": USER_ID,
            "cache_key": CACHE_KEY,
            "analysis_mode": "BOTH",
            "locked_at": (scaduto - timedelta(minutes=30)).isoformat(),
            "expires_at": scaduto.isoformat(),
        },
    )

    assert await lock_service.acquire(USER_ID, CACHE_KEY, "BOTH", get_settings())


async def test_un_lock_ancora_valido_non_viene_sottratto(store: FakeStore) -> None:
    """Il rovescio: se la scadenza fosse ignorata, il mutex non esisterebbe."""
    store.seed(
        "analysis_locks",
        {
            "user_id": USER_ID,
            "cache_key": CACHE_KEY,
            "analysis_mode": "BOTH",
            "locked_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
    )

    assert not await lock_service.acquire(USER_ID, CACHE_KEY, "BOTH", get_settings())


async def test_il_lock_viene_rilasciato_quando_l_analisi_fallisce(
    app: Any,
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
) -> None:
    """Un'analisi fallita non deve lasciare la chiave occupata fino al TTL."""
    settings = get_settings()
    gemini.error = GeminiError()

    with pytest.raises(GeminiError):
        await _analizza(
            user_id=USER_ID, video_url=VIDEO_URL, mode="BOTH",
            settings=settings, gemini=gemini, apify=apify,
        )

    assert store.count("analysis_locks") == 0
    assert store.count("insights") == 0


async def test_il_lock_viene_rilasciato_quando_l_analisi_riesce(
    app: Any,
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
) -> None:
    settings = get_settings()

    await _analizza(
        user_id=USER_ID, video_url=VIDEO_URL, mode="BOTH",
        settings=settings, gemini=gemini, apify=apify,
    )

    assert store.count("analysis_locks") == 0


async def test_attesa_scaduta_risponde_409(
    app: Any,
    store: FakeStore,
    gemini: FakeGemini,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chi aspetta non aspetta all'infinito.

    Il lock è tenuto da un'analisi che non finisce entro l'attesa: la seconda
    richiesta si arrende con un errore esplicito, non con un 500 né restando
    appesa. L'analisi altrui prosegue, e una richiesta successiva la troverà
    in cache.
    """
    monkeypatch.setenv("ANALYSIS_LOCK_WAIT_SECONDS", "0")
    monkeypatch.setenv("ANALYSIS_LOCK_POLL_SECONDS", "0.01")
    get_settings.cache_clear()
    settings = get_settings()

    store.seed(
        "analysis_locks",
        {
            "user_id": USER_ID,
            "cache_key": CACHE_KEY,
            "analysis_mode": "BOTH",
            "locked_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
    )

    with pytest.raises(AnalysisInProgressError) as errore:
        await _analizza(
            user_id=USER_ID, video_url=VIDEO_URL, mode="BOTH",
            settings=settings, gemini=gemini, apify=apify,
        )

    assert errore.value.status_code == 409
    # Nessuna spesa: chi si arrende non deve aver chiamato nulla.
    assert len(gemini.calls) == 0
