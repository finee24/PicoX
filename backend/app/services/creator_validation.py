"""Verifica che un account esista e sia pubblico, prima di monitorarlo.

Tre livelli, e l'ordine fra loro è la parte che conta:

1. **parsing** — da `@tizio` o da un URL di profilo a `(piattaforma, handle)`;
2. **cache** (`creator_validations`, TTL 24h) — un hit non costa nulla e non
   consuma quota;
3. **quota** (`validation_events`, migration `0010`) — si consuma *solo* quando
   si sta per chiamare davvero un provider a consumo.

## Perché la cache da sola non basta

È lo stesso ragionamento della quota sulle analisi (voce A3 dell'audit), su un
endpoint diverso. La cache azzera il costo delle chiamate ripetute **sullo
stesso identificatore**, e quella è la ripetizione che accade per errore o per
comodità. Il vettore d'abuso è l'altro: 5.000 handle *diversi* sono 5.000 cache
miss, cioè 5.000 chiamate esterne pagate. Solo il tetto per utente chiude quel
caso, ed è il motivo per cui questo endpoint non sarebbe pronto al merge con la
sola cache.

## Perché qui non c'è un lock

`perform_analysis` protegge la pipeline con `analysis_locks` perché due
richieste concorrenti sullo stesso video costerebbero due volte ~$0,035 di
Gemini più Apify. Una validazione costa ~$0,003 e dura un round-trip: il lock
costerebbe due scritture su database per risparmiarne, nel caso peggiore, una
frazione di centesimo. La finestra fra due validazioni concorrenti sullo stesso
handle esiste, è breve, e la si paga volentieri.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import PicoxValidationError
from app.schemas.creators import (
    CreatorProfilePreview,
    CreatorValidationResponse,
    Platform,
    clean_username,
)
from app.services.apify_service import ApifyService
from app.services.provider_parsing import come_datetime
from app.services.supabase_service import (
    db_errors,
    service_table,
    unscoped_service_table,
)
from app.services.youtube_service import YouTubeService, is_channel_id

logger = logging.getLogger(__name__)

_TABELLA_CACHE: Final = "creator_validations"

# Host → piattaforma. È un'allowlist di host **esatti** per la stessa ragione
# spiegata in `apify_service._PLATFORM_HOSTS`: un confronto per sottostringa
# classificherebbe `instagram.com.evil.example` come Instagram, e manderebbe la
# richiesta di un terzo all'actor sbagliato.
#
# **Non è una copia di quella tabella, e non va allineata a quella.** Qui
# mancano di proposito `youtu.be`, `vm.tiktok.com` e `vt.tiktok.com`: in quegli
# URL il primo segmento di path è un id di contenuto, che il parser dei profili
# tratterebbe come handle — validando un creator che non esiste. Le due tabelle
# rispondono a domande diverse (quale actor invocare, contro cos'è un profilo
# valido) e devono poter divergere.
_HOST_PIATTAFORMA: Final[dict[str, Platform]] = {
    "instagram.com": "instagram",
    "www.instagram.com": "instagram",
    "m.instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
    "m.tiktok.com": "tiktok",
    "youtube.com": "youtube_shorts",
    "www.youtube.com": "youtube_shorts",
    "m.youtube.com": "youtube_shorts",
}

# Primi segmenti di path che non sono mai un profilo. Servono a dare un errore
# comprensibile — «questo è il link di un video» — invece di andare a chiedere
# al provider un account che si chiama `reel`.
_SEGMENTI_NON_PROFILO: Final[frozenset[str]] = frozenset(
    {
        "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
        "watch", "shorts", "results", "feed", "playlist", "video", "hashtag",
        "tag", "music", "discover", "foryou", "live",
    }
)

# Prefissi di path YouTube che introducono un identificatore di canale.
_PREFISSI_CANALE: Final[frozenset[str]] = frozenset({"channel", "c", "user"})


class TargetValidazione:
    """`(piattaforma, identificatore)` già normalizzati."""

    __slots__ = ("identifier", "platform")

    def __init__(self, platform: Platform, identifier: str) -> None:
        self.platform = platform
        self.identifier = identifier


def _normalizza_handle(raw: str, *, minuscolo: bool = True) -> str:
    """Handle in forma canonica: senza '@', minuscolo.

    Minuscolo perché su tutte e tre le piattaforme l'handle è
    case-insensitive: senza, `@Tizio` e `@tizio` sarebbero due righe di cache
    per lo stesso profilo, e la seconda ripagherebbe il provider. È anche il
    valore che il client passa poi a `POST /api/v1/creators`, dove una forma
    unica evita due creator per lo stesso account.

    `minuscolo=False` serve all'unico identificatore che **non** è un handle:
    l'id di canale YouTube (`UC…`), che è case-sensitive. Abbassarlo lo
    renderebbe irrisolvibile, e il canale risulterebbe inesistente.
    """
    try:
        pulito = clean_username(raw)
    except ValueError as exc:
        raise PicoxValidationError(str(exc)) from exc
    return pulito.lower() if minuscolo else pulito


def _handle_da_url(url: str) -> TargetValidazione:
    """Piattaforma e handle estratti da un URL di profilo."""
    parti = urlsplit(url if "://" in url else f"https://{url}")

    host = (parti.hostname or "").lower().rstrip(".")
    piattaforma = _HOST_PIATTAFORMA.get(host)
    if piattaforma is None:
        raise PicoxValidationError(
            "Link non riconosciuto: incolla il link del profilo su Instagram, "
            "TikTok o YouTube, oppure scrivi direttamente l'username."
        )

    segmenti = [segmento for segmento in parti.path.split("/") if segmento]
    if not segmenti:
        raise PicoxValidationError(
            "Il link non contiene un profilo: aggiungi l'username, "
            "es. instagram.com/nomeutente."
        )

    primo = segmenti[0].lower()

    # `youtube.com/channel/UC…`, `/c/Nome`, `/user/Nome`: l'identificatore è il
    # segmento successivo, non il primo.
    if piattaforma == "youtube_shorts" and primo in _PREFISSI_CANALE:
        if len(segmenti) < 2:
            raise PicoxValidationError("Il link del canale YouTube è incompleto.")
        # Solo `/channel/` introduce un **id** (`UC…`), che è case-sensitive.
        # `/c/` e `/user/` introducono nomi vanity, che si comportano come
        # handle e vanno abbassati come tutti gli altri.
        return TargetValidazione(
            piattaforma,
            _normalizza_handle(segmenti[1], minuscolo=primo != "channel"),
        )

    # Un segmento che inizia con '@' è l'autore, anche quando il resto del path
    # è un contenuto: `tiktok.com/@tizio/video/123` porta con sé l'handle di
    # chi l'ha pubblicato, ed è un'informazione vera che non ha senso rifiutare.
    # L'asimmetria col ramo qui sotto non è una svista: da
    # `instagram.com/reel/Cxyz` l'autore non è ricavabile, quindi lì l'unica
    # risposta onesta è chiedere il profilo.
    if primo.startswith("@"):
        return TargetValidazione(piattaforma, _normalizza_handle(segmenti[0]))

    if primo in _SEGMENTI_NON_PROFILO:
        raise PicoxValidationError(
            "Questo è il link di un contenuto, non di un profilo. Incolla il "
            "link dell'account oppure scrivi l'username."
        )

    return TargetValidazione(piattaforma, _normalizza_handle(segmenti[0]))


def parse_creator_input(raw: str, platform: Platform | None) -> TargetValidazione:
    """Da ciò che l'utente ha scritto a `(piattaforma, handle)`.

    Un URL porta con sé la piattaforma e quella vince sul parametro: se
    qualcuno incolla un link Instagram avendo lasciato "TikTok" selezionato nel
    menu, l'intenzione vera è nel link. È anche ciò che permette
    all'interfaccia di aggiornare da sé il selettore dopo un incolla.

    Un handle nudo invece **non è deducibile** — `@tizio` esiste su tutte e tre
    le piattaforme — e senza `platform` produce un 422 che lo dice. L'alternativa
    (provare tutte le piattaforme) triplicherebbe la spesa esterna per ogni
    richiesta ambigua, cioè esattamente ciò che questo modulo esiste per
    contenere.
    """
    testo = raw.strip()
    if not testo:
        raise PicoxValidationError("Scrivi un username o incolla il link di un profilo.")

    # Un handle può contenere punti (`mario.rossi`), quindi la presenza di un
    # punto non basta a dire "è un URL": si guardano schema, path e prefisso
    # `www.`, che un handle non può avere.
    sembra_url = "://" in testo or "/" in testo or testo.lower().startswith("www.")
    if sembra_url:
        return _handle_da_url(testo)

    if platform is None:
        raise PicoxValidationError(
            "Indica la piattaforma, oppure incolla il link completo del profilo: "
            "lo stesso username può esistere su Instagram, TikTok e YouTube."
        )

    # Un id di canale YouTube è case-sensitive, e qui arriva **nudo**: il ramo
    # dell'URL lo riconosce dal prefisso `/channel/`, questo non ha alcun
    # prefisso da guardare se non l'identificatore stesso. Senza questa riga
    # `UCaaa…` diventava `ucaaa…`, non superava più `_is_channel_id`, finiva su
    # `forHandle` e il canale risultava inesistente — un "non esiste" su un
    # account che esiste, cioè il difetto che questa verifica esiste per evitare.
    return TargetValidazione(
        platform, _normalizza_handle(testo, minuscolo=not is_channel_id(testo.lstrip("@")))
    )


# =============================================================================
# Cache
# =============================================================================


async def _leggi_cache(
    target: TargetValidazione, settings: Settings
) -> CreatorValidationResponse | None:
    """Riga di cache ancora fresca, `None` se assente o scaduta."""
    # `di_routine`: questa lettura scatta a **ogni** richiesta di validazione,
    # cache hit compresi. Lasciarla a INFO rendeva il log delle query non
    # scopate un rumore di fondo, e un segnale che scatta sempre non segnala
    # più niente. La scrittura qui sotto resta a INFO di proposito: avviene solo
    # quando un provider è stato davvero pagato, quindi è rara e vale la pena
    # vederla.
    tabella = await unscoped_service_table(
        _TABELLA_CACHE,
        reason="cache condivisa delle validazioni: la chiave non ha un utente",
        di_routine=True,
    )

    async with db_errors("lettura cache validazioni"):
        risultato = await (
            tabella.select("response,checked_at")
            .eq("platform", target.platform)
            .eq("normalized_identifier", target.identifier)
            .limit(1)
            .execute()
        )

    if not risultato.data:
        return None

    # PostgREST restituisce JSON arbitrario: la riga è un oggetto solo finché
    # non lo si verifica, e un `isinstance` qui costa meno di un 500 il giorno
    # in cui non lo è.
    riga = risultato.data[0]
    if not isinstance(riga, dict):
        return None

    verificato = come_datetime(riga.get("checked_at"))
    if verificato is None:
        return None

    scadenza = datetime.now(UTC) - timedelta(hours=settings.creator_validation_ttl_hours)
    if verificato < scadenza:
        logger.info(
            "Cache di validazione scaduta per %s/%s: si rivalida.",
            target.platform,
            target.identifier,
        )
        return None

    try:
        return CreatorValidationResponse.model_validate(riga.get("response"))
    except ValidationError:
        # Riga scritta da una versione precedente dello schema. Si tratta come
        # un miss e si rivalida: sollevare qui trasformerebbe un'evoluzione dello
        # schema in un 422 per ogni utente che tocca quel creator, e la riga
        # verrebbe comunque sovrascritta dalla rivalidazione.
        logger.info(
            "Riga di cache non conforme allo schema per %s/%s: si rivalida.",
            target.platform,
            target.identifier,
        )
        return None


async def _scrivi_cache(
    target: TargetValidazione, esito: CreatorValidationResponse
) -> None:
    """Aggiorna la riga di cache. Non solleva: un fallimento non è fatale.

    La validazione è già stata pagata e il risultato è in mano al chiamante:
    trasformare un errore di scrittura in un 503 butterebbe via proprio la
    chiamata che si è appena pagata. Il costo di un fallimento è che la
    prossima richiesta ripaghi il provider, che è il comportamento di prima
    della cache.
    """
    payload: dict[str, Any] = {
        "platform": target.platform,
        "normalized_identifier": target.identifier,
        "response": esito.model_dump(mode="json"),
        "checked_at": esito.checked_at.isoformat(),
    }

    try:
        tabella = await unscoped_service_table(
            _TABELLA_CACHE,
            reason="scrittura cache condivisa delle validazioni",
        )
        await tabella.upsert(
            payload, on_conflict="platform,normalized_identifier"
        ).execute()
    except Exception:  # vedi docstring: qui nulla deve propagarsi
        logger.warning(
            "Scrittura della cache di validazione fallita per %s/%s.",
            target.platform,
            target.identifier,
            exc_info=True,
        )


# =============================================================================
# Quota
# =============================================================================


def _arrotonda_alla_finestra(momento: datetime, ttl_ore: int) -> datetime:
    """Tronca `momento` all'inizio della finestra di TTL che lo contiene.

    ## Perché l'istante esatto non può uscire da qui

    `creator_validations` è una cache **condivisa fra tutti gli utenti**: è ciò
    che le dà valore, perché due utenti interessati allo stesso creator pagano il
    provider una volta sola. Ma significa che su un cache hit il `checked_at`
    restituito è l'istante in cui **qualcun altro** ha validato quell'handle.

    Con l'istante al secondo, chiunque può interrogare un handle e leggere quando
    è stato verificato l'ultima volta: un oracolo su cosa stanno guardando gli
    altri utenti di Picox. Sul singolo handle dice poco; su una lista di handle
    interrogata a intervalli regolari disegna l'attività altrui.

    Arrotondare alla finestra del TTL toglie quella risoluzione lasciando intatto
    ciò che serve davvero al client — «quanto è vecchio questo dato» — perché
    oltre la finestra il dato non esiste più comunque: viene rivalidato.

    Il valore **persistito** resta esatto: qui si arrotonda solo ciò che esce
    verso il client. La scadenza della cache si calcola sulla colonna
    `checked_at` della riga, non su questo campo, e falsarla sposterebbe il TTL
    fino a 24 ore.
    """
    secondi_finestra = max(ttl_ore, 1) * 3600
    epoca = datetime(1970, 1, 1, tzinfo=UTC)
    trascorsi = int((momento - epoca).total_seconds())
    return epoca + timedelta(seconds=trascorsi - (trascorsi % secondi_finestra))


def _senza_istante_esatto(
    esito: CreatorValidationResponse, settings: Settings
) -> CreatorValidationResponse:
    """La risposta come la vede il client, con `checked_at` arrotondato.

    Si applica a **entrambi** i rami, cache hit e validazione fresca, e non solo
    al primo. Arrotondare solo i cache hit lascerebbe in piedi lo stesso oracolo
    in forma più sottile: un valore al secondo direbbe «questa l'ho pagata io»,
    uno arrotondato «l'aveva già validata qualcun altro» — cioè esattamente il
    fatto che si vuole nascondere.
    """
    return esito.model_copy(
        update={
            "checked_at": _arrotonda_alla_finestra(
                esito.checked_at, settings.creator_validation_ttl_hours
            )
        }
    )


async def _consuma_quota(user_id: str, target: TargetValidazione) -> None:
    """Registra la validazione che sta per essere pagata, o la rifiuta.

    Va chiamata **dopo** il controllo di cache e **prima** della chiamata al
    provider, perché è lì che la spesa è decisa. Le due alternative sbagliano in
    direzioni opposte, come per le analisi: più in alto si conterebbero i cache
    hit, che non costano nulla; più in basso non si conterebbero le chiamate che
    partono e falliscono — le stesse che l'actor ha comunque eseguito e fatturato.

    Non decide nulla da sé: l'arbitro è il trigger `enforce_validation_quota`
    (migration `0010`), che solleva `PX003` tradotto in `ValidationQuotaError`.
    Così due richieste concorrenti al tetto non possono superarlo entrambe.
    """
    eventi = await service_table("validation_events", user_id)
    async with db_errors("quota giornaliera di validazioni"):
        await eventi.insert(
            {
                "platform": target.platform,
                "normalized_identifier": target.identifier,
            }
        ).execute()


# =============================================================================
# Interrogazione dei provider
# =============================================================================


def _da_profilo_apify(profilo: Any, target: TargetValidazione) -> CreatorValidationResponse:
    return CreatorValidationResponse(
        platform=target.platform,
        normalized_identifier=target.identifier,
        exists=True,
        # L'unico caso in cui un account esiste e non è utilizzabile. Un profilo
        # privato non è un errore da nascondere: la card lo mostra e spiega
        # perché non si può monitorare.
        is_public=not profilo.is_private,
        profile=CreatorProfilePreview(
            avatar_url=profilo.avatar_url,
            display_name=profilo.display_name or profilo.username,
            username=profilo.username,
            is_verified=profilo.is_verified,
            follower_count=profilo.follower_count,
        ),
        checked_at=datetime.now(UTC),
    )


def _non_trovato(target: TargetValidazione) -> CreatorValidationResponse:
    return CreatorValidationResponse(
        platform=target.platform,
        normalized_identifier=target.identifier,
        exists=False,
        # Un account che non esiste non è "privato": `is_public` è falso perché
        # non c'è nulla di pubblico, e `exists` è il campo che distingue i due
        # casi. Il client non deve dedurre l'uno dall'altro.
        is_public=False,
        profile=None,
        checked_at=datetime.now(UTC),
    )


async def _interroga_provider(
    target: TargetValidazione,
    *,
    apify: ApifyService,
    youtube: YouTubeService,
) -> CreatorValidationResponse:
    """Chiama il provider giusto per la piattaforma."""
    if target.platform == "youtube_shorts":
        canale = await youtube.fetch_channel(target.identifier)
        if canale is None:
            return _non_trovato(target)

        return CreatorValidationResponse(
            platform=target.platform,
            # L'handle canonico secondo YouTube (`customUrl`) può differire da
            # quello scritto: si tiene quello, così la cache e il creator creato
            # dopo puntano allo stesso valore.
            normalized_identifier=canale.handle.lower() or target.identifier,
            exists=True,
            # Un canale con un handle che risolve è pubblico per costruzione:
            # YouTube non ha canali visibili ai soli iscritti. Vedi la docstring
            # di `youtube_service`.
            is_public=True,
            profile=CreatorProfilePreview(
                avatar_url=canale.avatar_url,
                display_name=canale.title,
                username=canale.handle,
                # La Data API non espone lo stato di verifica in `channels.list`.
                # `False` qui significa "non confermato", non "non verificato".
                is_verified=False,
                follower_count=canale.subscriber_count,
            ),
            checked_at=datetime.now(UTC),
        )

    profilo = await apify.fetch_profile(target.platform, target.identifier)
    if profilo is None:
        return _non_trovato(target)
    return _da_profilo_apify(profilo, target)


# =============================================================================
# API del modulo
# =============================================================================


async def validate_creator(
    *,
    user_id: str,
    raw_input: str,
    platform: Platform | None,
    settings: Settings,
    apify: ApifyService,
    youtube: YouTubeService,
) -> CreatorValidationResponse:
    """Verifica un account, servendo dalla cache quando possibile."""
    target = parse_creator_input(raw_input, platform)

    in_cache = await _leggi_cache(target, settings)
    if in_cache is not None:
        logger.info(
            "Cache hit sulla validazione di %s/%s", target.platform, target.identifier
        )
        return _senza_istante_esatto(in_cache, settings)

    await _consuma_quota(user_id, target)

    esito = await _interroga_provider(target, apify=apify, youtube=youtube)
    # La riga si scrive sotto la chiave **cercata**, non sotto quella canonica
    # che il provider può aver restituito (il `customUrl` di YouTube). Sono la
    # stessa cosa in tutti i casi tranne quello, e lì la conseguenza è una
    # seconda riga di cache per lo stesso canale — non un errore: chi arriva
    # dall'alias trova comunque la risposta giusta, che porta con sé
    # l'identificatore canonico.
    # L'arrotondamento avviene **dopo** la scrittura, non prima: in cache va
    # l'istante esatto, perché è su quello che si calcola la scadenza del TTL.
    # Invertire l'ordine sposterebbe la scadenza fino a 24 ore.
    await _scrivi_cache(target, esito)
    return _senza_istante_esatto(esito, settings)
