"""Il prompt di analisi vive in un file, non nel codice.

`analysis_prompt.yaml` tiene la parte che si riscrive spesso — il ruolo del
modello, le regole, le istruzioni per piattaforma, il blocco di contesto — e
`build_analysis_prompt` la compone con le sezioni per modalità
(`info.md`, `style.md`, `script.md`), che restano file a sé perché cambiano per
conto proprio.

## Cosa **non** sta nel YAML

La forma della risposta. La impone `response_schema=VideoAnalysisResponse`, che
`google-genai` converte in JSON Schema e invia insieme al video: due schemi in
disaccordo — uno nel prompt, uno nella richiesta — producono una risposta che
non valida, cioè un 503 dopo aver pagato l'inferenza due volte. Nel file si
scrive il criterio; il contratto è il modello Pydantic.

## Caption e hashtag sono testo di terzi

Il contesto contiene la caption scritta dal creator del video, che non è
l'utente di Picox e non ha alcun rapporto con lui: è un input non fidato che
finisce nello stesso prompt delle istruzioni. Due cose lo tengono a bada:

* il `context_template` lo delimita in apertura **e** in chiusura, e dopo la
  chiusura rimette l'ultima parola al sistema — la caption è l'ultimo testo del
  prompt, la posizione da cui un'iniezione peserebbe di più;
* la sostituzione avviene con `str.format` sul *template*, mai sui valori: una
  caption che contiene `{...}` finisce nel prompt come testo, non come
  segnaposto da risolvere. È il motivo per cui non serve sanificarla oltre.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Final, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import GeminiError
from app.schemas.analysis import AnalysisMode, InfoAnalysis
from app.schemas.creators import Platform
from app.schemas.scraping import ScraperResult

logger = logging.getLogger(__name__)

# I prompt vivono fuori dal codice applicativo: si iterano senza toccare i
# service (vedi il subagent .claude/agents/prompt-tuner.md).
_PROMPTS_DIR: Final = Path(__file__).resolve().parents[2] / "prompts"
PROMPT_CONFIG_PATH: Final = _PROMPTS_DIR / "analysis_prompt.yaml"

_SEPARATORE: Final = "\n\n---\n\n"

# Le piattaforme limitano le caption ben sotto questa soglia (2.200 caratteri su
# Instagram e TikTok). Il taglio non serve contro il creator, serve contro un
# actor Apify che restituisca un blob al posto di una caption: senza, una
# risposta anomala si trasformerebbe in token pagati a ogni analisi.
_MAX_CAPTION_CHARS: Final = 2000
_MAX_HASHTAG: Final = 30
_HASHTAG_RE: Final = re.compile(r"#(\w+)")

# `Platform` è la nomenclatura interna; nel prompt va il nome che il modello
# riconosce. "youtube_shorts" non è una parola che esiste fuori da questo repo.
_ETICHETTA_PIATTAFORMA: Final[dict[str, str]] = {
    "instagram": "Instagram Reel",
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Short",
}


def _formati_ammessi() -> list[str]:
    """Il vocabolario di `content_format`, letto dallo schema che lo impone.

    Non è una duplicazione della `Literal`: ne è *la* lettura. Il modello lo
    riceve già come `enum` nel JSON Schema, ma ripeterlo nel testo aiuta
    l'aderenza — e ripeterlo copiandolo a mano in un YAML avrebbe creato una
    seconda lista libera di divergere, che è il modo tipico in cui un prompt
    inizia a chiedere valori che poi la validazione rifiuta.
    """
    literal = InfoAnalysis.model_fields["content_format"].annotation
    return [str(valore) for valore in get_args(literal)]


class PlatformOverride(BaseModel):
    """Istruzioni aggiuntive per una singola piattaforma."""

    model_config = ConfigDict(extra="forbid")

    extra_instructions: str


class AnalysisPromptConfig(BaseModel):
    """Contenuto di `analysis_prompt.yaml`, validato al caricamento."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    base_prompt: str
    # Le chiavi sono valori di `Platform`: una chiave sconosciuta fa fallire il
    # caricamento invece di restare un override che non viene mai applicato.
    platform_overrides: dict[Platform, PlatformOverride] = Field(default_factory=dict)
    allowed_categories: list[str] = Field(default_factory=_formati_ammessi)
    context_template: str


class AnalysisContext(BaseModel):
    """Ciò che si sa del video **oltre** ai suoi fotogrammi.

    Tutto opzionale: un link diretto a un `.mp4` non ha né autore né caption, e
    in quel caso il blocco di contesto non viene aggiunto affatto.
    """

    model_config = ConfigDict(extra="forbid")

    platform: Platform
    creator_username: str = ""
    caption: str | None = ""
    hashtags: list[str] = Field(default_factory=list)

    @classmethod
    def da_scraping(cls, scraped: ScraperResult) -> AnalysisContext:
        """Costruisce il contesto da un risultato di scraping.

        L'autore viene reso in due modi diversi ed è voluto: su Instagram e
        TikTok `author_username` è un handle e va scritto con la `@`, su YouTube
        è il **nome** del canale (`videos.list` non espone l'handle, vedi
        `content_scraper.py`) e anteporgli una `@` lo trasformerebbe in un
        riferimento che non esiste.
        """
        autore = scraped.author_username.strip()
        if autore and scraped.platform != "youtube_shorts":
            autore = f"@{autore.lstrip('@')}"

        return cls(
            platform=scraped.platform,
            creator_username=autore,
            caption=scraped.caption,
            hashtags=estrai_hashtag(scraped.caption),
        )


def estrai_hashtag(caption: str | None) -> list[str]:
    """Gli hashtag presenti nella caption, senza `#`, in ordine e senza duplicati.

    Si ricavano dalla caption invece di chiederli allo scraper: gli actor li
    espongono in campi diversi e non sempre, mentre nella caption ci sono per
    definizione — è lì che il creator li ha scritti.
    """
    if not caption:
        return []

    visti: dict[str, None] = {}
    for tag in _HASHTAG_RE.findall(caption):
        visti.setdefault(tag.lower(), None)
        if len(visti) >= _MAX_HASHTAG:
            break
    return list(visti)


def _tronca(testo: str | None) -> str:
    if not testo:
        return ""
    pulito = testo.strip()
    if len(pulito) <= _MAX_CAPTION_CHARS:
        return pulito
    logger.info("Caption troncata a %s caratteri per il prompt.", _MAX_CAPTION_CHARS)
    return f"{pulito[:_MAX_CAPTION_CHARS]} […caption troncata]"


def _riempi(template: str, **valori: str) -> str:
    """`str.format` che non fa saltare un'analisi per una graffa di troppo.

    I segnaposto stanno nel template, che è nostro; i valori sostituiti **non**
    vengono rielaborati, quindi una caption con delle graffe passa come testo.
    Il ripiego serve al caso in cui il template non sia più nostro:
    `user_override_base_prompt` un giorno arriverà da una tabella Supabase, e
    una graffa scritta da un utente non deve poter trasformare la sua analisi in
    un 500.
    """
    try:
        return template.format(**valori)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "Segnaposto non risolto nel prompt (%s): sezione lasciata invariata.",
            type(exc).__name__,
        )
        return template


@lru_cache(maxsize=8)
def _load_section(name: str) -> str:
    """Una sezione per modalità, da `backend/prompts/<name>.md`."""
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("Prompt '%s' non leggibile.", name, exc_info=True)
        raise GeminiError(details=None) from exc


@lru_cache(maxsize=4)
def load_prompt_config(path: Path = PROMPT_CONFIG_PATH) -> AnalysisPromptConfig:
    """Legge e valida `analysis_prompt.yaml`.

    In cache: il file non cambia a runtime, e rileggerlo a ogni analisi
    significherebbe un accesso a disco per inferenza. Dopo una modifica al YAML
    va riavviato il processo — la stessa regola che vale già per le sezioni
    `.md`.

    Un file mancante, un YAML malformato o una chiave fuori schema diventano un
    `GeminiError` (503): senza prompt non c'è analisi, e un 500 con lo stack
    trace direbbe all'utente cose che non lo riguardano.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error("Configurazione del prompt non leggibile: %s", path, exc_info=True)
        raise GeminiError(details=None) from exc
    except yaml.YAMLError as exc:
        logger.error("Configurazione del prompt non è YAML valido: %s", path, exc_info=True)
        raise GeminiError(details=None) from exc

    try:
        return AnalysisPromptConfig.model_validate(raw)
    except ValidationError as exc:
        logger.error("Configurazione del prompt non conforme: %s", exc)
        raise GeminiError(details=None) from exc


def _sezioni_per_modalita(mode: AnalysisMode) -> list[str]:
    """Le sole sezioni pertinenti, più l'elenco dei campi da lasciare vuoti.

    Il modello riceve istruzioni solo per ciò che deve produrre; le sezioni
    escluse sono elencate esplicitamente come `null` attesi, perché uno schema
    con un campo nullable non basta a impedire che venga riempito.
    """
    sezioni: list[str] = []
    attesi_null: list[str] = []

    if mode in ("INFO", "BOTH"):
        sezioni.append(_load_section("info"))
    else:
        attesi_null.append("info_analysis")

    if mode in ("STYLE", "BOTH"):
        sezioni.append(_load_section("style"))
    else:
        attesi_null.append("style_analysis")

    if mode == "BOTH":
        sezioni.append(_load_section("script"))
    else:
        attesi_null.append("inverse_script")

    if attesi_null:
        campi = ", ".join(f"`{nome}`" for nome in attesi_null)
        sezioni.append(
            f"## Campi da lasciare vuoti\n\n"
            f"Questa analisi è in modalità **{mode}**. I campi {campi} devono essere "
            f"`null`. Non compilarli in nessun caso, nemmeno parzialmente."
        )

    return sezioni


def build_analysis_prompt(
    mode: AnalysisMode,
    context: AnalysisContext | None = None,
    config: AnalysisPromptConfig | None = None,
    user_override_base_prompt: str | None = None,
) -> str:
    """Il prompt completo per una singola analisi.

    L'ordine è: base, sezioni della modalità, istruzioni della piattaforma,
    contesto. Il contesto sta in fondo perché è l'unica parte che contiene testo
    di terzi, e il template che lo racchiude si chiude rimettendo l'ultima parola
    al sistema.

    `mode` non compare nella specifica originale di questa funzione ed è stato
    aggiunto qui: senza, le sezioni per modalità resterebbero fuori dal prompt e
    un'analisi `INFO` chiederebbe anche lo stile.

    `user_override_base_prompt` non è ancora usato da nessuno. Esiste perché la
    personalizzazione per utente — un `base_prompt` salvato su Supabase — possa
    arrivare senza toccare il file condiviso: sostituisce **solo** la base, e
    lascia intatte regole di modalità, override di piattaforma e delimitazione
    del contesto, che non sono personalizzabili proprio perché sono ciò che
    tiene la risposta dentro lo schema.
    """
    cfg = config or load_prompt_config()

    base = user_override_base_prompt or cfg.base_prompt
    base = _riempi(base, allowed_categories=", ".join(cfg.allowed_categories))

    sezioni: list[str] = [base, *_sezioni_per_modalita(mode)]

    if context is not None:
        override = cfg.platform_overrides.get(context.platform)
        if override is not None:
            sezioni.append(override.extra_instructions)

        sezioni.append(
            _riempi(
                cfg.context_template,
                platform=_ETICHETTA_PIATTAFORMA.get(context.platform, context.platform),
                creator_username=context.creator_username or "(non disponibile)",
                caption=_tronca(context.caption) or "(nessuna caption)",
                hashtags=", ".join(context.hashtags) if context.hashtags else "(nessuno)",
            )
        )

    return _SEPARATORE.join(sezione.strip() for sezione in sezioni if sezione.strip())
