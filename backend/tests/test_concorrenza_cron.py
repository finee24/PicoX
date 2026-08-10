"""Esecuzioni sovrapposte del job cron.

Il censimento di `POST /api/v1/cron/check-updates` non ha coordinazione: due
esecuzioni che si accavallano scrapano entrambe ogni creator attivo, e ognuna
porta con sé il proprio `asyncio.Semaphore`, quindi il parallelismo delle
analisi in background raddoppia rispetto al limite configurato.

DA DOVE ARRIVA LA SOVRAPPOSIZIONE. Non dallo schedule: nella configurazione
documentata in `app/cron_config.md` un secondo giro schedulato si mette in coda
(`concurrency: picox-cron`). Arriva dal **retry del client**: `curl --max-time
120 --retry 2` abortisce e ri-POSTa se il censimento supera i 120s, mentre il
server sta ancora elaborando la prima richiesta — stessa run, stesso gruppo di
concorrenza, nessuna guardia. Il censimento e' sequenziale (`cron.py`, un
`await fetch_latest_videos` per creator), quindi superare 120s al tetto di 30
creator attivi non e' un caso limite.

I numeri della baseline, misurati prima della correzione, sono citati in ogni
test: senza, l'asserzione non direbbe che qualcosa e' cambiato.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

import app.api.v1.cron as cron_module
from app.core.config import get_settings
from app.services.job_lock import CRON_CHECK_UPDATES
from tests.conftest import TEST_CRON_SECRET, USER_ID, FakeApify, FakeGemini
from tests.fake_supabase import FakeStore

CRON_HEADERS = {"X-CRON-SECRET": TEST_CRON_SECRET}


def _semina_creator(store: FakeStore, quanti: int) -> None:
    for i in range(quanti):
        store.seed(
            "creators",
            {
                "user_id": USER_ID,
                "username": f"creator_{i}",
                "platform": "tiktok",
                "analysis_mode": "INFO",
                "is_active": True,
            },
        )


def _video(apify: FakeApify, quanti: int) -> None:
    from app.services.apify_service import ScrapedVideo

    apify.videos = [
        ScrapedVideo(
            video_url=f"https://tiktok.com/@creator/video/{i}",
            download_url=f"https://cdn.example.com/video-{i}.mp4",
            thumbnail_url=None,
            duration_seconds=30.0,
        )
        for i in range(quanti)
    ]


def _censimento_lento(apify: FakeApify, secondi: float = 0.08) -> None:
    """Apify reale impiega secondi per creator.

    Senza attesa la prima esecuzione finirebbe il censimento prima che la
    seconda cominci, e il test sarebbe verde per assenza di sovrapposizione
    invece che per correttezza.
    """
    originale = apify.fetch_latest_videos

    async def lento(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(secondi)
        return await originale(*args, **kwargs)

    apify.fetch_latest_videos = lento  # type: ignore[method-assign]


class _Parallelismo:
    """Misura il picco di analisi in volo, avvolgendo `perform_analysis`."""

    def __init__(self) -> None:
        self.in_volo = 0
        self.picco = 0
        self.chiamate = 0

    def installa(self, monkeypatch: pytest.MonkeyPatch, ritardo: float = 0.05) -> None:
        async def contata(**kwargs: Any) -> tuple[None, bool]:
            self.chiamate += 1
            self.in_volo += 1
            self.picco = max(self.picco, self.in_volo)
            try:
                await asyncio.sleep(ritardo)
                return None, False
            finally:
                self.in_volo -= 1

        monkeypatch.setattr(cron_module, "perform_analysis", contata)


async def _due_esecuzioni_sovrapposte(app: Any) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return list(
            await asyncio.gather(
                c.post("/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60),
                c.post("/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60),
            )
        )


# =============================================================================
# 1. Lo scraping duplicato
# =============================================================================


async def test_due_esecuzioni_sovrapposte_scrapano_ogni_creator_una_volta_sola(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BASELINE PRIMA DELLA CORREZIONE: 3 creator -> 6 fetch, non 3.

    `fetch_latest_videos` e' una chiamata esterna a pagamento: ogni esecuzione
    in piu' e' una run di Apify in piu', per ogni creator di ogni utente.
    """
    _semina_creator(store, 3)
    _video(apify, 2)
    _censimento_lento(apify)
    _Parallelismo().installa(monkeypatch)

    risposte = await _due_esecuzioni_sovrapposte(app)

    assert all(r.status_code == 200 for r in risposte), [r.text for r in risposte]
    assert len(apify.fetch_calls) == 3, (
        f"{len(apify.fetch_calls)} scraping per 3 creator: la seconda esecuzione "
        "non ha saltato il giro (prima della correzione erano 6)"
    )


async def test_la_seconda_esecuzione_salta_il_giro_e_lo_dichiara(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Saltare non basta: deve essere visibile in produzione.

    Un cron che salta in silenzio e' indistinguibile da un cron che non parte.
    """
    _semina_creator(store, 3)
    _video(apify, 1)
    _censimento_lento(apify)
    _Parallelismo().installa(monkeypatch)

    with caplog.at_level("INFO"):
        risposte = await _due_esecuzioni_sovrapposte(app)

    saltate = [r for r in risposte if r.json().get("skipped")]
    assert len(saltate) == 1, "esattamente una delle due deve saltare"
    assert len([r for r in risposte if not r.json().get("skipped")]) == 1

    # `getMessage()` e non `message % args`: il record porta il template e gli
    # argomenti separati, ed e' il logging a saperli comporre.
    messaggi = " ".join(rec.getMessage() for rec in caplog.records)
    assert "giro saltato" in messaggi, messaggi
    assert any(rec.levelname == "WARNING" and "giro saltato" in rec.getMessage()
               for rec in caplog.records), "il salto deve essere un WARNING, non un INFO"


# =============================================================================
# 2. Il parallelismo raddoppiato
# =============================================================================


async def test_il_parallelismo_resta_entro_il_limite_configurato(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BASELINE PRIMA DELLA CORREZIONE: picco 4 con limite configurato 2.

    Ogni esecuzione costruisce il proprio `asyncio.Semaphore`, quindi il limite
    vale per esecuzione e non per processo: due sovrapposte lo raddoppiano.
    """
    monkeypatch.setenv("CRON_MAX_CONCURRENT_ANALYSES", "2")
    get_settings.cache_clear()

    _semina_creator(store, 3)
    _video(apify, 4)
    _censimento_lento(apify)
    misura = _Parallelismo()
    misura.installa(monkeypatch)

    await _due_esecuzioni_sovrapposte(app)

    limite = get_settings().cron_max_concurrent_analyses
    assert misura.picco <= limite, (
        f"picco di {misura.picco} analisi in volo con limite {limite} "
        "(prima della correzione il picco arrivava a 4)"
    )


# =============================================================================
# 3. Nessun effetto collaterale sullo stato
# =============================================================================


async def test_nessuna_riga_duplicata_dopo_la_sovrapposizione(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lo stato scritto resta uno solo per video, e il lock non resta orfano."""
    _semina_creator(store, 2)
    _video(apify, 2)
    _censimento_lento(apify)

    await _due_esecuzioni_sovrapposte(app)

    insights = store.rows("insights")
    chiavi = [(r["user_id"], r["video_url"]) for r in insights]
    assert len(chiavi) == len(set(chiavi)), f"righe duplicate in insights: {chiavi}"


# =============================================================================
# 4. Il lock copre il background, non la sola richiesta HTTP
# =============================================================================


async def test_il_lock_resta_preso_per_tutta_la_durata_del_background(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BackgroundTasks` disaccoppia la risposta dal lavoro: il lock no.

    La 200 parte a fine censimento, ma il giro non e' finito — le analisi
    proseguono per minuti. Se il rilascio fosse legato alla richiesta HTTP, in
    quella finestra il lock sarebbe libero e un secondo giro ripartirebbe
    mentre il primo sta ancora analizzando: esattamente il problema che questa
    correzione chiude, spostato piu' avanti nel tempo.

    La prova non e' "la riga del lock esiste" ma "un secondo giro **vero**,
    lanciato da dentro il background, viene respinto".
    """
    _semina_creator(store, 1)
    _video(apify, 1)

    osservato: dict[str, Any] = {}
    transport = httpx.ASGITransport(app=app)

    async def durante_analisi(**kwargs: Any) -> tuple[None, bool]:
        # Siamo dentro il BackgroundTask: la risposta del primo giro e' gia'
        # partita.
        osservato["righe_lock"] = len(store.rows("job_locks"))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60
            )
        osservato["secondo_giro"] = r.json()
        osservato["scraping_del_secondo"] = len(apify.fetch_calls)
        return None, False

    monkeypatch.setattr(cron_module, "perform_analysis", durante_analisi)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        prima = await c.post(
            "/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60
        )

    assert prima.status_code == 200, prima.text
    assert prima.json()["skipped"] is False
    assert prima.json()["queued_analyses"] == 1

    # Durante l'analisi in background il lock era ancora preso...
    assert osservato["righe_lock"] == 1, "il lock era gia' stato rilasciato"
    # ...e un secondo giro lanciato in quel momento e' stato respinto.
    assert osservato["secondo_giro"]["skipped"] is True
    # ...senza aver scrapato nulla: 1 solo censimento in tutto.
    assert osservato["scraping_del_secondo"] == 1

    # A background concluso il lock e' libero: il giro successivo non trovera'
    # un lock appeso da aspettare fino al TTL.
    assert store.rows("job_locks") == []


async def test_il_lock_viene_rilasciato_anche_senza_analisi_da_accodare(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
) -> None:
    """Senza job non c'e' background task a cui cedere il lock.

    E' il ramo in cui il rilascio resta nella route: se ci si dimenticasse, un
    giro a vuoto — nessun video nuovo, il caso piu' frequente — bloccherebbe
    tutti i giri successivi fino alla scadenza del TTL.
    """
    _semina_creator(store, 1)
    apify.videos = []

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        risposta = await c.post(
            "/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60
        )

    assert risposta.json()["queued_analyses"] == 0
    assert store.rows("job_locks") == [], "lock non rilasciato su un giro a vuoto"


# =============================================================================
# 5. Deploy o crash a meta' giro
# =============================================================================


def _lock_orfano(store: FakeStore, *, scaduto_da: timedelta | None,
                 fra: timedelta | None = None) -> None:
    """Il lock lasciato da un processo morto, senza alcun `finally` eseguito."""
    adesso = datetime.now(UTC)
    scadenza = adesso - scaduto_da if scaduto_da is not None else adesso + (fra or timedelta())
    store.seed(
        "job_locks",
        {
            "job_name": CRON_CHECK_UPDATES,
            "locked_at": (scadenza - timedelta(seconds=1800)).isoformat(),
            "expires_at": scadenza.isoformat(),
            "holder": "istanza-morta-nel-deploy/1",
        },
    )


async def test_un_lock_orfano_scaduto_non_blocca_il_giro_successivo(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Il processo muore a meta' giro; il giro dopo deve ripartire.

    E' il caso che il TTL esiste per coprire, e non puo' dipendere da un
    `finally`: un container riavviato a meta' deploy non esegue nulla in uscita.

    L'aritmetica che rende il caso sempre risolto: il lock scade 1800s dopo
    essere stato preso, la finestra successiva arriva dopo 6h. 6h > 1800s con un
    fattore 12, quindi un giro schedulato trova **sempre** il lock scaduto — non
    esiste una combinazione di tempi in cui resti bloccato.
    """
    _semina_creator(store, 2)
    _video(apify, 1)
    _Parallelismo().installa(monkeypatch)
    # Il processo e' morto 30 minuti fa: il TTL di 1800s e' gia' trascorso.
    _lock_orfano(store, scaduto_da=timedelta(seconds=1))

    with caplog.at_level("WARNING"):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            risposta = await c.post(
                "/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60
            )

    assert risposta.json()["skipped"] is False, "bloccato da un lock abbandonato"
    assert risposta.json()["checked_creators"] == 2
    assert len(apify.fetch_calls) == 2

    # La sottrazione di un lock scaduto non e' un evento normale: va registrata,
    # altrimenti un processo che muore a ogni giro resta invisibile.
    messaggi = " ".join(rec.getMessage() for rec in caplog.records)
    assert "scaduto e sottratto" in messaggi, messaggi


async def test_un_lock_ancora_valido_blocca_il_giro_successivo(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Il gruppo di controllo del test qui sopra.

    Senza, "il giro riparte" sarebbe compatibile con un'acquisizione che ignora
    del tutto la scadenza e sottrae sempre il lock — cioe' con un lock che non
    protegge nulla. Qui la scadenza e' nel futuro e il giro **deve** fermarsi.
    """
    _semina_creator(store, 2)
    _video(apify, 1)
    _Parallelismo().installa(monkeypatch)
    _lock_orfano(store, scaduto_da=None, fra=timedelta(seconds=1800))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        risposta = await c.post(
            "/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60
        )

    assert risposta.json()["skipped"] is True
    assert apify.fetch_calls == [], "ha scrapato pur avendo saltato il giro"


# =============================================================================
# 6. La guardia: CRON_ENABLED
# =============================================================================


async def test_il_cron_spento_risponde_503_con_codice_dedicato(
    app: Any,
    store: FakeStore,
    apify: FakeApify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spento per difetto: il segreto giusto non basta ad accenderlo."""
    monkeypatch.setenv("CRON_ENABLED", "false")
    get_settings.cache_clear()
    _semina_creator(store, 1)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        risposta = await c.post(
            "/api/v1/cron/check-updates", headers=CRON_HEADERS, timeout=60
        )

    assert risposta.status_code == 503, risposta.text
    assert risposta.json()["error"]["code"] == "cron_disabled"
    assert apify.fetch_calls == [], "ha scrapato con il cron disabilitato"
    assert store.rows("job_locks") == [], "ha preso il lock con il cron disabilitato"


async def test_senza_segreto_il_cron_spento_risponde_401_non_503(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'ordine delle dipendenze non e' un dettaglio di stile.

    `verify_cron_enabled` sta dopo `verify_cron_secret`: invertendole, chiunque
    senza segreto potrebbe distinguere `503 cron_disabled` da `401` e dedurre lo
    stato di configurazione dell'istanza. Prima ci si autentica, poi si scopre
    che e' spento.
    """
    monkeypatch.setenv("CRON_ENABLED", "false")
    get_settings.cache_clear()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        senza = await c.post("/api/v1/cron/check-updates", timeout=60)
        sbagliato = await c.post(
            "/api/v1/cron/check-updates",
            headers={"X-CRON-SECRET": "non-e-il-segreto"},
            timeout=60,
        )

    for risposta in (senza, sbagliato):
        assert risposta.status_code == 401, risposta.text
        assert risposta.json()["error"]["code"] == "unauthorized"
        assert "cron_disabled" not in risposta.text
