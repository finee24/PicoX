"""Schemi dell'analisi video passati a Gemini come `response_schema`.

Questi modelli **sono** parte del prompt: `google-genai` li converte in JSON
Schema e li invia al modello insieme al video, quindi ogni `description` è
un'istruzione che il modello legge. Da qui la regola di progetto — nessun campo
senza `Field(description=...)`, nessun `dict[str, Any]`, enum espressi come
`Literal` — documentata in `.claude/skills/gemini-structured-output/SKILL.md`.

Mappatura verso il database (`public.insights`):

    info_analysis   -> summary_data            (jsonb)
    style_analysis  -> style_data              (jsonb)
    inverse_script  -> inverse_script_template (jsonb)
    keywords        -> keywords                (text[])
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AnalysisMode = Literal["INFO", "STYLE", "BOTH"]
"""Modalità di analisi. Allineata al CHECK constraint di `insights.analysis_mode`."""


class InfoAnalysis(BaseModel):
    """Payload INFO — *cosa* dice il video. Finisce in `insights.summary_data`."""

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(
        description=(
            "Sintesi del contenuto del video in 3-5 frasi, in italiano. Descrive ciò "
            "che viene effettivamente detto e mostrato, senza giudizi di valore e "
            "senza formule introduttive come 'in questo video'."
        )
    )
    main_topic: str = Field(
        description=(
            "Argomento principale in massimo 8 parole, in italiano. Specifico e non "
            "generico: 'routine serale per la pelle grassa', non 'skincare'."
        )
    )
    key_points: list[str] = Field(
        description=(
            "Da 3 a 6 affermazioni sostanziali fatte nel video, in ordine di "
            "apparizione, ciascuna in una frase autoconclusiva in italiano. Solo "
            "contenuti effettivamente presenti: nessuna inferenza o completamento."
        )
    )
    target_audience: str = Field(
        description=(
            "Destinatario del contenuto descritto in una frase, dedotto da linguaggio, "
            "riferimenti e livello di competenza presupposto. In italiano."
        )
    )
    value_proposition: str = Field(
        description=(
            "In una frase, il beneficio concreto che lo spettatore ottiene guardando "
            "il video (cosa impara, risolve o scopre). In italiano."
        )
    )
    content_format: Literal[
        "tutorial",
        "storytelling",
        "listicle",
        "opinion",
        "review",
        "demonstration",
        "interview",
        "reaction",
        "promotional",
        "other",
    ] = Field(
        description=(
            "Formato editoriale dominante del video. Scegliere 'other' solo se "
            "nessuna delle categorie precedenti descrive il video."
        )
    )
    keywords: list[str] = Field(
        description=(
            "Da 5 a 10 keyword tematiche in minuscolo, una o due parole ciascuna, "
            "senza cancelletto e senza duplicati. Servono per la ricerca: usare i "
            "termini con cui un utente cercherebbe questo contenuto."
        )
    )


class StyleAnalysis(BaseModel):
    """Payload STYLE — *come* lo dice. Finisce in `insights.style_data`."""

    model_config = ConfigDict(extra="ignore")

    hook: str = Field(
        description=(
            "Trascrizione letterale delle parole pronunciate o mostrate a schermo nei "
            "primi 3 secondi. Mantenere la lingua originale del video, senza tradurre."
        )
    )
    hook_type: Literal[
        "question",
        "bold_claim",
        "curiosity_gap",
        "problem_agitation",
        "story_opening",
        "demonstration",
        "shock_visual",
        "direct_address",
        "other",
    ] = Field(
        description="Meccanismo con cui i primi secondi trattengono lo spettatore."
    )
    hook_duration_seconds: float = Field(
        ge=0,
        le=30,
        description=(
            "Durata in secondi del segmento iniziale che funge da hook, cioè fino al "
            "primo cambio di ritmo, inquadratura o argomento."
        ),
    )
    pacing: Literal["slow", "moderate", "fast", "very_fast"] = Field(
        description=(
            "Ritmo percepito, basato sulla frequenza dei tagli e sulla velocità del "
            "parlato: 'slow' inquadrature lunghe e parlato disteso, 'very_fast' tagli "
            "sotto il secondo e parlato accelerato."
        )
    )
    average_shot_duration_seconds: float = Field(
        ge=0,
        description=(
            "Durata media di una singola inquadratura in secondi, stimata contando i "
            "tagli visibili e dividendo la durata totale per il loro numero."
        ),
    )
    editing_techniques: list[str] = Field(
        description=(
            "Da 2 a 6 tecniche di montaggio osservabili (es. 'jump cut', 'zoom "
            "progressivo', 'b-roll sovrapposto', 'match cut sul beat'), in italiano. "
            "Solo tecniche effettivamente visibili nel video."
        )
    )
    visual_style: str = Field(
        description=(
            "Descrizione in 1-2 frasi di inquadrature, illuminazione, palette di "
            "colori e ambientazione. In italiano."
        )
    )
    text_overlay_usage: str = Field(
        description=(
            "Come viene usato il testo a schermo: presenza, posizione, tipo (sottotitoli "
            "integrali, parole chiave enfatizzate, nessuno) e funzione narrativa. Se non "
            "è presente alcun testo, dirlo esplicitamente. In italiano."
        )
    )
    audio_style: str = Field(
        description=(
            "Componente sonora in 1-2 frasi: presenza di voce fuori campo, musica di "
            "sottofondo e suo ruolo, effetti sonori, silenzi usati come pausa. In italiano."
        )
    )
    tone_of_voice: str = Field(
        description=(
            "Registro comunicativo in 3-6 parole (es. 'diretto e confidenziale', "
            "'ironico e provocatorio'). In italiano."
        )
    )
    cta: str | None = Field(
        default=None,
        description=(
            "Trascrizione letterale della call to action, nella lingua originale del "
            "video. `null` se il video non contiene alcun invito esplicito all'azione: "
            "non inventarne una."
        ),
    )
    retention_devices: list[str] = Field(
        description=(
            "Da 1 a 5 espedienti usati per trattenere lo spettatore fino alla fine "
            "(es. 'promessa risolta solo nel finale', 'countdown a schermo', 'loop "
            "sull'inquadratura iniziale'), in italiano."
        )
    )


class ScriptBeat(BaseModel):
    """Singolo blocco narrativo dello script inverso."""

    model_config = ConfigDict(extra="ignore")

    timestamp: str = Field(
        description=(
            "Intervallo temporale del blocco nel formato 'm:ss-m:ss', es. '0:00-0:03'."
        )
    )
    section: Literal["hook", "context", "development", "payoff", "cta"] = Field(
        description="Funzione narrativa del blocco all'interno della struttura del video."
    )
    content: str = Field(
        description=(
            "Cosa accade nel blocco a livello di contenuto: sintesi di ciò che viene "
            "detto o mostrato, in una o due frasi. In italiano."
        )
    )
    purpose: str = Field(
        description=(
            "Perché questo blocco esiste, cioè l'effetto che produce sullo spettatore "
            "(crea tensione, dà prova, scioglie la curiosità). In italiano."
        )
    )
    visual_direction: str = Field(
        description=(
            "Indicazione registica riutilizzabile per rigirare il blocco: tipo di "
            "inquadratura, movimento, elementi a schermo. In italiano."
        )
    )


class InverseScript(BaseModel):
    """Struttura riusabile derivata dal video. Finisce in `insights.inverse_script_template`.

    È un *template*: descrive lo scheletro riadattabile a un altro argomento, non
    una trascrizione del video originale.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(
        description=(
            "Nome dello schema narrativo in massimo 10 parole, formulato in modo "
            "riutilizzabile (es. 'Problema comune, errore diffuso, metodo in 3 passi'). "
            "In italiano."
        )
    )
    total_duration_seconds: float = Field(
        ge=0,
        description="Durata complessiva del video originale in secondi.",
    )
    beats: list[ScriptBeat] = Field(
        description=(
            "Da 3 a 8 blocchi narrativi in ordine cronologico, che coprono l'intera "
            "durata del video senza sovrapposizioni."
        )
    )
    reusable_hook_template: str = Field(
        description=(
            "Hook riscritto come modello compilabile, con i riferimenti specifici "
            "sostituiti da segnaposto tra parentesi graffe, es. 'Se {pubblico} sta "
            "ancora facendo {errore}, sta perdendo {conseguenza}'. In italiano."
        )
    )
    reusable_cta_template: str | None = Field(
        default=None,
        description=(
            "Call to action riscritta come modello con segnaposto tra parentesi graffe. "
            "`null` se il video originale non contiene alcuna CTA."
        ),
    )
    adaptation_notes: str = Field(
        description=(
            "In 2-3 frasi: quali elementi rendono efficace questa struttura e a quali "
            "tipi di argomento si trasferisce bene. In italiano."
        )
    )


class VideoAnalysisResponse(BaseModel):
    """Contratto completo restituito da Gemini per un singolo video.

    Le sezioni non richieste dalla modalità di analisi devono essere `null`:
    il prompt lo esplicita e la validazione applicativa lo verifica in
    `app.services.gemini_service`.
    """

    model_config = ConfigDict(extra="ignore")

    info_analysis: InfoAnalysis | None = Field(
        default=None,
        description=(
            "Analisi del contenuto. Valorizzata solo nelle modalità INFO e BOTH; "
            "`null` in modalità STYLE."
        ),
    )
    style_analysis: StyleAnalysis | None = Field(
        default=None,
        description=(
            "Analisi della forma. Valorizzata solo nelle modalità STYLE e BOTH; "
            "`null` in modalità INFO."
        ),
    )
    inverse_script: InverseScript | None = Field(
        default=None,
        description=(
            "Script inverso riutilizzabile. Valorizzato solo in modalità BOTH; "
            "`null` nelle modalità INFO e STYLE."
        ),
    )
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Da 5 a 10 keyword in minuscolo per la ricerca trasversale, valorizzate in "
            "ogni modalità. In modalità INFO e BOTH coincidono con "
            "`info_analysis.keywords`; in modalità STYLE descrivono forma e formato "
            "(es. 'jump cut', 'voce fuori campo', 'tutorial')."
        ),
    )


class AnalyzeVideoRequest(BaseModel):
    """Body di `POST /api/v1/analyze-video`.

    Nota: nessun `user_id`. L'identità arriva dal JWT verificato — accettarla dal
    body sarebbe un IDOR.
    """

    model_config = ConfigDict(extra="forbid")

    video_url: str = Field(
        min_length=8,
        max_length=2048,
        description="URL pubblico del video da analizzare (Reel, TikTok o YouTube Short).",
    )
    analysis_mode: AnalysisMode = Field(
        default="BOTH",
        description="INFO: solo contenuto. STYLE: solo forma. BOTH: entrambe più script inverso.",
    )
