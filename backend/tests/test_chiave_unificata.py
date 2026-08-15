"""La stessa chiave in tutti e quattro i punti che decidono se pagare.

Dalla migration `0009` l'identita' di un video e' `insights.cache_key`, distinta
da `video_url` che resta il valore **mostrato** — finisce in un `href`
cliccabile, quindi deve restare un URL navigabile.

IL CASO CHE MOTIVA TUTTO. Lo stesso video TikTok e' raggiungibile con **username
diversi** nel path: `tiktok.com/@tizio/video/123` e `tiktok.com/@caio/video/123`
aprono entrambi il video `123`. Come URL da mostrare sono due valori legittimi e
distinti; come identita' sono lo stesso video.

**Se anche uno solo dei quattro punti usasse un valore diverso**, quelle due
richieste passerebbero per chiavi diverse e la spesa tornerebbe doppia. Da qui un
test per ciascuno, invece di uno solo sulla funzione che calcola la chiave:

  1. la cache di lettura   (`find_cached_insight`)
  2. il target dell'upsert (`ON CONFLICT`)
  3. il lock              (`analysis_locks`)
  4. il dedup del cron    (`_filter_already_analyzed`)
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.api.v1.cron import _filter_already_analyzed
from app.core.config import get_settings
from app.services.analysis_service import find_cached_insight, perform_analysis
from app.services.media_service import canonical_cache_key, normalize_video_url
from tests.conftest import USER_ID, FakeApify, FakeGemini, FakeYouTube
from tests.fake_supabase import FakeStore, make_jwt

# Stesso video, due username diversi nel path: entrambi aprono il video 123.
URL_A = "https://www.tiktok.com/@tizio/video/123"
URL_B = "https://tiktok.com/@caio/video/123/?is_from_webapp=1"
CHIAVE = "tiktok.com:123"


async def _analizza(**kwargs: Any) -> Any:
    """I doppi non implementano i protocolli reali: il confine di tipo sta qui.

    Stesso schema di `test_concorrenza_analisi.py`, compreso il doppio YouTube
    che qui non viene mai interrogato: gli URL sono TikTok.
    """
    kwargs.setdefault("youtube", FakeYouTube())
    return await perform_analysis(**kwargs)


# =============================================================================
# 0. La premessa, con il gruppo di controllo
# =============================================================================


def test_i_due_url_sono_diversi_ma_la_chiave_e_la_stessa() -> None:
    """Senza questo, i quattro test sotto sarebbero verdi anche se i due URL
    fossero identici — cioe' senza provare nulla."""
    assert normalize_video_url(URL_A) != normalize_video_url(URL_B), (
        "gli URL mostrati devono restare distinti: sono entrambi legittimi"
    )
    assert canonical_cache_key(URL_A) == canonical_cache_key(URL_B) == CHIAVE


def test_la_chiave_non_e_un_url() -> None:
    """Deliberato: nessuno deve essere tentato di metterla in un `href`."""
    assert not CHIAVE.startswith("http")


# =============================================================================
# 1. La cache di lettura
# =============================================================================


async def test_la_cache_trova_il_video_salvato_sotto_l_altro_url(
    store: FakeStore,
) -> None:
    store.seed(
        "insights",
        {
            "user_id": USER_ID,
            "video_url": normalize_video_url(URL_A),
            "cache_key": CHIAVE,
            "analysis_mode": "INFO",
            "summary_data": {"main_topic": "x"},
            # In Postgres una colonna esiste sempre: il doppio non la aggiunge
            # da se' su `seed`, e senza queste la validazione del modello di
            # risposta fallirebbe per un motivo inventato dal test.
            "creator_id": None,
            "thumbnail_url": None,
        },
    )

    trovato = await find_cached_insight(
        USER_ID, canonical_cache_key(URL_B),
        access_token=None, settings=get_settings(),
    )

    assert trovato is not None, "cercando con l'altra forma dell'URL non si trova"
    # E cio' che si mostra resta l'URL navigabile archiviato, non la chiave.
    assert str(trovato.video_url).startswith("https://")


# =============================================================================
# 2. Il target dell'upsert
# =============================================================================


async def test_due_analisi_sequenziali_sui_due_url_danno_una_riga_sola(
    store: FakeStore, gemini: FakeGemini, apify: FakeApify
) -> None:
    """Il secondo passaggio deve essere un cache hit, non una seconda riga."""
    settings = get_settings()

    _, da_cache_a = await _analizza(
        user_id=USER_ID, video_url=URL_A, mode="INFO",
        settings=settings, gemini=gemini, apify=apify,
    )
    _, da_cache_b = await _analizza(
        user_id=USER_ID, video_url=URL_B, mode="INFO",
        settings=settings, gemini=gemini, apify=apify,
    )

    assert da_cache_a is False
    assert da_cache_b is True, "la seconda forma non ha colpito la cache"
    assert len(store.rows("insights")) == 1, "due righe per lo stesso video"
    assert len(gemini.calls) == 1, "Gemini pagato due volte per lo stesso video"


async def test_una_modalita_diversa_aggiorna_la_riga_senza_duplicarla(
    store: FakeStore, gemini: FakeGemini, apify: FakeApify
) -> None:
    """L'upsert deve colpire il vincolo nuovo, non crearne una seconda.

    E il fix dell'omissione di `creator_id` deve reggere sulla nuova clausola:
    la riga aveva un creator, la rianalisi manuale non ne passa uno, e
    l'attribuzione non deve sparire.
    """
    settings = get_settings()
    creator = "11111111-2222-3333-4444-555555555555"

    await _analizza(
        user_id=USER_ID, video_url=URL_A, mode="INFO",
        settings=settings, gemini=gemini, apify=apify, creator_id=creator,
    )
    await _analizza(
        user_id=USER_ID, video_url=URL_B, mode="STYLE",
        settings=settings, gemini=gemini, apify=apify,
    )

    righe = store.rows("insights")
    assert len(righe) == 1, "l'upsert ha creato una seconda riga"
    assert righe[0]["creator_id"] == creator, (
        "l'attribuzione al creator e' stata cancellata: il fix dell'omissione "
        "non regge sulla clausola ON CONFLICT nuova"
    )


# =============================================================================
# 3. Il lock — punto 5 del prompt
# =============================================================================


async def test_richieste_concorrenti_sui_due_url_pagano_una_sola_analisi(
    app: Any, store: FakeStore, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Due richieste in parallelo sullo stesso video, con username diversi.

    E' il caso che il lock ri-chiavato dalla `0009` esiste per coprire: prima,
    `video_url` diversi davano **lock distinti**, entrambe procedevano ed
    entrambe pagavano — il difetto della `0003` riaperto dalla porta di
    servizio.

    Gemini e' rallentato di proposito: senza attesa la prima finirebbe prima che
    la seconda arrivi al lock, e il test sarebbe verde per assenza di
    concorrenza invece che per correttezza.
    """
    monkeypatch.setenv("ANALYSIS_LOCK_POLL_SECONDS", "0.02")
    get_settings.cache_clear()

    originale = gemini.analyze_video

    async def lento(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.15)
        return await originale(*args, **kwargs)

    gemini.analyze_video = lento  # type: ignore[method-assign]

    from tests.conftest import TEST_JWT_SECRET

    headers = {"Authorization": f"Bearer {make_jwt(USER_ID, TEST_JWT_SECRET)}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        risposte = await asyncio.gather(
            c.post("/api/v1/analyze-video", headers=headers, timeout=60,
                   json={"video_url": URL_A, "analysis_mode": "INFO"}),
            c.post("/api/v1/analyze-video", headers=headers, timeout=60,
                   json={"video_url": URL_B, "analysis_mode": "INFO"}),
        )

    assert all(r.status_code in (200, 201) for r in risposte), [r.text for r in risposte]
    assert len(gemini.calls) == 1, (
        f"{len(gemini.calls)} inferenze per lo stesso video: il lock non ha "
        "arbitrato sulla chiave unificata"
    )
    assert len(store.rows("insights")) == 1
    # Una ha analizzato (201), l'altra ha atteso e restituito il risultato (200).
    assert sorted(r.status_code for r in risposte) == [200, 201]


# =============================================================================
# 4. Il dedup del cron
# =============================================================================


async def test_il_cron_non_riaccoda_un_video_gia_analizzato_sotto_altra_forma(
    store: FakeStore,
) -> None:
    """Riaccodarlo significherebbe ripagarlo, sul percorso automatico."""
    store.seed(
        "insights",
        {
            "user_id": USER_ID,
            "video_url": normalize_video_url(URL_A),
            "cache_key": CHIAVE,
            "analysis_mode": "BOTH",
        },
    )

    nuovi = await _filter_already_analyzed(USER_ID, [URL_B])

    assert nuovi == [], "il cron riaccoderebbe un video gia' in archivio"


async def test_il_cron_accoda_ancora_i_video_davvero_nuovi(
    store: FakeStore,
) -> None:
    """Gruppo di controllo: senza, il test sopra sarebbe verde anche con una
    funzione che scarta tutto."""
    store.seed(
        "insights",
        {
            "user_id": USER_ID,
            "video_url": normalize_video_url(URL_A),
            "cache_key": CHIAVE,
            "analysis_mode": "BOTH",
        },
    )
    altro = "https://www.tiktok.com/@tizio/video/999"

    nuovi = await _filter_already_analyzed(USER_ID, [URL_B, altro])

    assert nuovi == [altro]


async def test_il_cron_restituisce_gli_url_originali_non_le_chiavi(
    store: FakeStore,
) -> None:
    """Il chiamante risale ai metadati gia' scaricati con `by_url`: se qui
    tornassero le chiavi, quel lookup fallirebbe in silenzio e nessun video
    verrebbe accodato."""
    nuovi = await _filter_already_analyzed(USER_ID, [URL_A, URL_B])

    assert all(u.startswith("http") for u in nuovi), nuovi
    assert set(nuovi) <= {URL_A, URL_B}
