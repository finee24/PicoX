#!/usr/bin/env python
"""UserPromptSubmit — aggancia le voci di `bug.md` pertinenti al prompt.

Quando il prompt sembra parlare di un bug si inietta l'**indice** di `bug.md`
(economico, sempre) più il **testo integrale** delle voci il cui titolo condivide
un termine col prompt (mirato).

Non si inietta mai il file intero: è destinato a crescere e l'output di un hook
è tagliato a 10.000 caratteri. Iniettare tutto significherebbe un troncamento
silenzioso, cioè perdere proprio le voci più vecchie — le più probabili a
contenere il precedente che si sta cercando.

Se il match fallisce non succede nulla: resta l'istruzione in `CLAUDE.md` («se il
task riguarda un bug, leggi `bug.md`»), che resta seguibile con gli strumenti di
lettura file. **Non blocca mai**: qualunque errore esce 0 senza contesto.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Vedi block_frontend_secrets.py: la console Windows non usa UTF-8 di default.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

PAROLE_BUG = [
    "bug", "errore", "error", "eccezione", "exception", "crash", "traceback",
    "stack trace", "non funziona", "rotto", "fallisce", "failing",
    "broken", "regressione", "regression", "debug",
    "non riesco", "non parte", "si blocca", "resta bloccat",
    "resta occupat", "non risponde",
]
CODICI_HTTP = re.compile(r"\b(500|503|422|401|409)\b")

# Parole troppo generiche per decidere QUALE voce iniettare per intero: "errore"
# o "bug" ricorrono in più titoli, e farle contare come match specifico
# aggancerebbe voci a caso solo perché il prompt le nomina in generale. Servono
# a decidere SE scattare (PAROLE_BUG sopra), non quale voce scegliere.
PAROLE_GENERICHE = {
    "bug", "errore", "eccezione", "exception", "crash", "traceback",
    "rotto", "fallisce", "failing", "broken", "regressione",
    "regression", "debug", "funziona",
}

LIMITE_CARATTERI = 9000  # margine sotto il tetto dichiarato di 10.000


def radice(dati: dict) -> Path:
    """Vedi `session_start_context.radice`: `cwd` può essere una sottocartella."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or dati.get("cwd") or ".")


def estrai_voci(testo: str) -> list[tuple[str, str]]:
    """[(titolo, corpo con intestazione), ...] per ogni `### ` **fuori** da un
    blocco di codice.

    Il filtro sui fence non è teorico: `bug.md` documenta il proprio formato con
    un `### [sintomo riconoscibile ...]` dentro un blocco ```, e uno split a
    espressione regolare lo raccoglie come se fosse una voce vera — indice
    sporcato, e un corpo di trenta righe di template pronto a essere iniettato
    al primo prompt che nomini "causa" o "sintomo".
    """
    voci: list[tuple[str, str]] = []
    titolo: str | None = None
    corrente: list[str] = []
    in_blocco = False

    for riga in testo.splitlines():
        if riga.lstrip().startswith("```"):
            in_blocco = not in_blocco
        elif not in_blocco and riga.startswith("### "):
            if titolo is not None:
                voci.append((titolo, "\n".join(corrente).rstrip()))
            titolo = riga.removeprefix("### ").strip()
            corrente = [riga]
            continue
        if titolo is not None:
            corrente.append(riga)

    if titolo is not None:
        voci.append((titolo, "\n".join(corrente).rstrip()))
    return voci


def frasi_backtick(titolo: str) -> list[str]:
    """Le frasi fra backtick del titolo, intere (`npm ci`, `503 gemini_unavailable`)."""
    return [t.strip().lower() for t in re.findall(r"`([^`]+)`", titolo) if t.strip()]


def token_backtick(titolo: str) -> list[str]:
    """I singoli token dentro i backtick.

    Serve perché nessuno scrive il titolo per intero: chi ha davanti un errore
    digita «ho un 503 su youtube», non «503 gemini_unavailable». Cercare solo la
    frase intera faceva mancare proprio la voce giusta — misurato, non temuto.
    Soglia a 3 caratteri: tiene `503` e `npm`, scarta `ci`, che come parola
    italiana comparirebbe ovunque.
    """
    token: list[str] = []
    for frase in frasi_backtick(titolo):
        token += [t for t in re.findall(r"[\w.]{3,}", frase) if t]
    return token


def punteggio(titolo: str, prompt: str, parole_prompt: set[str]) -> int:
    """Quanto la voce c'entra col prompt. 0 = non c'entra.

    Tre pesi, dal più al meno deliberato: la **frase** fra backtick citata per
    intero (10), un suo **token** (5), una **parola** qualsiasi in comune col
    titolo (2 l'una). Serve a ordinare le voci prima di iniettarle: se il budget
    finisce, a restare fuori dev'essere la meno pertinente, non l'ultima in
    ordine di file. Un punteggio >= 5 vale anche come innesco autonomo — vedi
    `componi`. Le parole pesano 2 e non 1 perché tre parole distintive in comune
    valgano da sole quanto un token: un titolo **senza backtick** — «Il riavvio di
    uvicorn su Windows non libera la porta» — altrimenti non potrebbe mai
    innescare da sé, e sarebbe raggiungibile solo dal vocabolario.
    """
    parole_titolo = set(re.findall(r"\w{4,}", titolo.lower())) - PAROLE_GENERICHE
    frasi = sum(1 for t in frasi_backtick(titolo) if t in prompt)
    token = sum(
        1
        for t in token_backtick(titolo)
        if re.search(r"\b" + re.escape(t) + r"\b", prompt)
    )
    return frasi * 10 + token * 5 + len(parole_prompt & parole_titolo) * 2


def componi(dati: dict) -> str | None:
    prompt = (dati.get("prompt") or "").lower()
    if not prompt:
        return None

    try:
        testo = (radice(dati) / "bug.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    voci = estrai_voci(testo)
    if not voci:
        return None

    parole_prompt = set(re.findall(r"\w{4,}", prompt)) - PAROLE_GENERICHE
    pesate = [
        (punteggio(titolo, prompt, parole_prompt), titolo, corpo)
        for titolo, corpo in voci
    ]

    # Due inneschi indipendenti, e il secondo è quello che conta di più.
    #
    # 1. **Vocabolario**: il prompt parla di un guasto in generale ("fallisce",
    #    "traceback", un codice HTTP). Ampio, e per forza incompleto: nessuna
    #    lista di parole copre tutti i modi di dire che qualcosa non va — la
    #    prova è che "check_env.py non trova le variabili" non scattava, pur
    #    nominando il titolo di una voce alla lettera.
    # 2. **Il prompt nomina qualcosa che sta in un titolo** (una frase fra
    #    backtick o un suo token, cioè punteggio >= 5). È un segnale più preciso
    #    del vocabolario e si mantiene da sé: cresce con bug.md, senza che
    #    nessuno debba ricordarsi di allungare una lista.
    segnale_vocabolario = any(p in prompt for p in PAROLE_BUG) or bool(
        CODICI_HTTP.search(prompt)
    )
    match_forte = any(p >= 5 for p, _, _ in pesate)
    if not (segnale_vocabolario or match_forte):
        return None

    indice = "\n".join(f"- {titolo}" for titolo, _ in voci)
    testa = (
        f"Il prompt sembra riguardare un bug — indice di bug.md "
        f"({len(voci)} voci):\n{indice}"
    )
    if len(testa) > LIMITE_CARATTERI:
        # Difesa per il giorno in cui le voci saranno tante: meglio un indice
        # tagliato di un JSON che il consumatore tronca a metà.
        return testa[:LIMITE_CARATTERI] + "\n[indice troncato]"

    parti = [testa]
    usati = len(testa)

    for _, _, corpo in sorted(
        (v for v in pesate if v[0] > 0), key=lambda v: v[0], reverse=True
    ):
        if usati + len(corpo) > LIMITE_CARATTERI:
            parti.append(
                "\n(altre voci in tema trovate ma non iniettate per restare "
                "sotto il limite di caratteri — apri bug.md a mano.)"
            )
            break
        parti.append("\n" + corpo)
        usati += len(corpo)

    return "\n".join(parti)


def main() -> None:
    try:
        dati = json.load(sys.stdin)
        if not isinstance(dati, dict):
            return
        contesto = componi(dati)
    except Exception:  # noqa: BLE001 — un hook sul prompt non solleva mai
        return

    if not contesto:
        return

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": contesto,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
