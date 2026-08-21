"""Eccezioni di dominio.

Ogni eccezione porta con sé lo status HTTP e un `code` stabile: i router
sollevano queste, gli handler registrati in `app.middleware.error_handler` le
traducono in risposte. Il messaggio è pensato per essere mostrato a un utente
finale — non contiene mai stack trace, query, URL interni o credenziali.
"""

from __future__ import annotations

from typing import Any


class PicoxError(Exception):
    """Base di tutte le eccezioni applicative."""

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "Si è verificato un errore imprevisto."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class AuthError(PicoxError):
    """JWT assente, scaduto o non verificabile; segreto cron errato."""

    status_code = 401
    code = "unauthorized"
    default_message = "Autenticazione richiesta o non valida."


class ForbiddenError(PicoxError):
    status_code = 403
    code = "forbidden"
    default_message = "Operazione non consentita."


class NotFoundError(PicoxError):
    status_code = 404
    code = "not_found"
    default_message = "Risorsa non trovata."


class ConflictError(PicoxError):
    """Violazione di un vincolo di unicità (es. creator già monitorato)."""

    status_code = 409
    code = "conflict"
    default_message = "La risorsa esiste già."


class PlanLimitError(PicoxError):
    """L'utente ha raggiunto un limite previsto dal proprio piano.

    409 e non 403: non è una questione di permessi — l'operazione sarebbe
    legittima — ma di stato attuale della risorsa, che confligge con la
    richiesta. Nemmeno 402, che dichiarerebbe un paywall dove oggi c'è solo un
    tetto tecnico.

    Il messaggio è scritto qui e non ripreso dal database: quello di Postgres
    resta nei log, come per ogni altro errore tradotto in questo modulo.
    """

    status_code = 409
    code = "plan_limit_reached"
    default_message = (
        "Hai raggiunto il numero massimo di creator attivi previsto dal tuo "
        "piano. Disattivane o rimuovine uno per aggiungerne un altro."
    )


class AnalysisQuotaError(PicoxError):
    """L'utente ha esaurito le analisi manuali previste per oggi.

    Distinta da `PlanLimitError` benché entrambe siano 409 e riguardino il
    piano: il client deve poter dire «hai esaurito le analisi di oggi» e non
    «hai troppi creator». Un solo codice per due limiti diversi produrrebbe
    messaggi generici proprio dove servono istruzioni precise.

    409 e non 429: non è una richiesta troppo frequente da rallentare, è una
    quota esaurita. Un 429 — specie con `Retry-After` — suggerirebbe un
    ritentativo che fallirebbe identico fino a domani.
    """

    status_code = 409
    code = "analysis_quota_reached"
    default_message = (
        "Hai raggiunto il numero massimo di analisi giornaliere previste dal tuo "
        "piano. La quota si azzera domani."
    )


class ValidationQuotaError(PicoxError):
    """L'utente ha esaurito le validazioni di creator previste per oggi.

    Terza sorella di `PlanLimitError` e `AnalysisQuotaError`, e distinta da
    entrambe per lo stesso motivo per cui quelle due sono distinte fra loro: le
    tre situazioni richiedono all'utente tre azioni diverse — rimuovere un
    creator, aspettare domani per analizzare, aspettare domani per validare — e
    un codice unico produrrebbe un messaggio generico proprio dove servono
    istruzioni precise.

    409 e non 429: non è una richiesta troppo frequente da rallentare, è una
    quota esaurita. Un `Retry-After` suggerirebbe un ritentativo che fallirebbe
    identico fino a domani.
    """

    status_code = 409
    code = "validation_quota_reached"
    default_message = (
        "Hai raggiunto il numero massimo di verifiche di account giornaliere "
        "previste dal tuo piano. La quota si azzera domani."
    )


class GlobalCapacityError(PicoxError):
    """Il tetto di spesa globale del servizio è stato raggiunto (migration 0011).

    **Il messaggio è deliberatamente generico, e non va reso più preciso.** Le
    tre sorelle qui sopra dicono all'utente esattamente quale limite ha
    raggiunto, perché è un limite *suo* e ha un rimedio suo. Questo no: è una
    rete di sicurezza sull'intero servizio, e dire «tetto di spesa globale
    raggiunto» rivelerebbe a chi sta sondando il sistema che esiste una soglia
    aggregata — cioè che riempirla è un modo di fermare il servizio per tutti,
    e quanto manca a riuscirci. Un attaccante che riceve «riprova più tardi»
    non impara nulla; uno che riceve «tetto globale a $100» ha una mappa.

    Per lo stesso motivo i numeri (spesa stimata, tetto) non compaiono qui:
    vivono solo nel WARNING strutturato che `translate_postgrest_error`
    registra nei log.

    409 e non 503, benché il messaggio somigli a quello di un guasto. Un 503
    dichiara un servizio non funzionante e invita al ritentativo — spesso
    automatico, e con `Retry-After` per convenzione — mentre questa condizione
    dura fino a mezzanotte UTC e i ritentativi la peggiorano. È la stessa
    ragione per cui `AnalysisQuotaError` è 409 e non 429. Tenerlo distinto da
    `DatabaseError` (503) serve anche al monitoraggio: un tetto raggiunto e un
    database irraggiungibile non sono lo stesso incidente.
    """

    status_code = 409
    code = "global_capacity_reached"
    default_message = (
        "Servizio momentaneamente non disponibile. Riprova più tardi."
    )


class AnalysisInProgressError(PicoxError):
    """Un'altra richiesta sta già analizzando questo video per questo utente.

    409 e non 425 o 503: la richiesta non è "troppo presto" né il servizio è
    indisponibile — è lo stato attuale della risorsa a rendere l'operazione
    superflua. Non è un errore da cui difendersi: l'analisi in corso arriverà a
    termine, e la richiesta successiva la troverà in cache.
    """

    status_code = 409
    code = "analysis_in_progress"
    default_message = (
        "Questo video è già in analisi. Riprova fra poco: il risultato sarà "
        "disponibile senza doverla rifare."
    )


class CronDisabledError(PicoxError):
    """Il job cron non è abilitato su questa istanza.

    503 e non 404: l'endpoint esiste ed è configurato correttamente: è
    l'istanza a non essere pronta a servirlo. Un 404 manderebbe chi configura lo
    scheduler a cercare un errore di routing o di versione dell'API, cioè a
    indagare il problema sbagliato.

    Il messaggio nomina la variabile da impostare di proposito: qui il
    destinatario non è un utente finale ma chi sta configurando il deploy, e
    `CRON_ENABLED` non è un segreto né un dettaglio implementativo — è
    documentata in `render.yaml` e in `cron_config.md`.
    """

    status_code = 503
    code = "cron_disabled"
    default_message = (
        "Il job cron non è abilitato su questa istanza. Impostare CRON_ENABLED=true "
        "dopo aver verificato che lo scheduler sia configurato correttamente."
    )


class PicoxValidationError(PicoxError):
    """Input non valido: body malformato, video oltre i limiti consentiti."""

    status_code = 422
    code = "validation_error"
    default_message = "I dati forniti non sono validi."


class VideoTooLargeError(PicoxValidationError):
    code = "video_too_large"
    default_message = "Il video supera il limite di dimensione consentito."


class VideoTooLongError(PicoxValidationError):
    code = "video_too_long"
    default_message = "Il video supera la durata massima consentita."


class ExternalServiceError(PicoxError):
    """Fallimento di un provider esterno (Gemini, Apify, download del media).

    503 e non 500: la causa è fuori dal nostro processo ed è tipicamente
    transitoria, quindi il client può ritentare.
    """

    status_code = 503
    code = "external_service_error"
    default_message = "Servizio esterno temporaneamente non disponibile."

    def __init__(
        self,
        message: str | None = None,
        *,
        service: str | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.service = service


class GeminiError(ExternalServiceError):
    code = "gemini_unavailable"
    default_message = (
        "L'analisi del video non è al momento disponibile. Riprova tra qualche minuto."
    )

    def __init__(self, message: str | None = None, *, details: Any | None = None) -> None:
        super().__init__(message, service="gemini", details=details)


class ApifyError(ExternalServiceError):
    code = "apify_unavailable"
    default_message = "Il recupero dei video dal creator non è al momento disponibile."

    def __init__(self, message: str | None = None, *, details: Any | None = None) -> None:
        super().__init__(message, service="apify", details=details)


class YouTubeError(ExternalServiceError):
    """Fallimento della YouTube Data API, o chiave non configurata.

    Codice proprio e non `apify_unavailable`: le due piattaforme passano da
    provider diversi, e un messaggio che parla di Apify manderebbe chi indaga
    un problema su YouTube a guardare il servizio sbagliato.

    Anche la chiave assente finisce qui, con un 503: per il client la
    piattaforma è indisponibile, ed è vero. Il fatto che la causa sia una
    variabile d'ambiente resta nei log, dove serve a chi configura il deploy —
    nel corpo della risposta sarebbe una descrizione dell'infrastruttura.
    """

    code = "youtube_unavailable"
    default_message = (
        "La verifica dei canali YouTube non è al momento disponibile. "
        "Riprova tra qualche minuto."
    )

    def __init__(self, message: str | None = None, *, details: Any | None = None) -> None:
        super().__init__(message, service="youtube", details=details)


class DatabaseError(PicoxError):
    status_code = 503
    code = "database_unavailable"
    default_message = "Il servizio dati non è al momento disponibile."
