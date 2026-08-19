#!/usr/bin/env python
"""SessionStart — inietta lo stato attivo (`context.md`) all'avvio della sessione.

Perché un hook e non la sola riga in `CLAUDE.md` («leggi `context.md` a inizio
sessione»): quella dipende dal fatto che l'istruzione venga letta e seguita.
Questo è deterministico — il contenuto è già nel contesto prima del primo
prompt. Gira su `startup`, `resume`, `clear` e `compact`: dopo una compattazione
lo stato attivo è la prima cosa che si perde.

**Non inietta `bug.md` per intero**, solo un puntatore economico (esiste, quante
voci). L'iniezione mirata è compito di `prompt_bug_context.py`, che scatta sui
prompt che sembrano parlare di un bug.

Protocollo: JSON su stdin, JSON su stdout con
`hookSpecificOutput.additionalContext`. **Non blocca mai**: qualunque errore
esce 0 senza contesto, perché un hook rotto non deve impedire l'avvio di una
sessione.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Vedi block_frontend_secrets.py: la console Windows non usa UTF-8 di default.
# Qui conta soprattutto **stdout**, che porta il JSON: `json.dumps` di suo
# produce ASCII puro (`ensure_ascii=True`), ma la riga rende la cosa esplicita
# invece che dipendente da un default che qualcuno potrebbe cambiare.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

LIMITE_CARATTERI = 9000  # margine sotto il tetto dichiarato di 10.000
# L'avviso su context.md è sui **caratteri**, non sulle righe, perché il
# vincolo vero è il budget di iniezione: a ~51 caratteri per riga il tetto di
# 9000 arriva intorno alle 175 righe, quindi una soglia "a 250 righe" non si
# accenderebbe mai — il troncamento la precederebbe sempre. Misurato il
# 19 agosto 2026: context.md era già a 8042 caratteri, l'89% del budget.
SOGLIA_AVVISO_CARATTERI = int(LIMITE_CARATTERI * 0.8)
SOGLIA_RIGHE_CLAUDE = 200  # soglia oltre cui l'aderenza alle istruzioni cala


def radice(dati: dict) -> Path:
    """La radice del progetto.

    `CLAUDE_PROJECT_DIR` prima di `cwd`: il secondo è la cartella da cui la
    sessione è partita, che può essere una sottocartella del repo — e in quel
    caso i tre file non si troverebbero.
    """
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or dati.get("cwd") or ".")


def righe_di(testo: str) -> int:
    """Numero di righe. `splitlines()` e non `count("\n") + 1`, che conta una
    riga di troppo su un file che finisce con newline — cioè su tutti."""
    return len(testo.splitlines())


def leggi(percorso: Path) -> str | None:
    try:
        return percorso.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def componi(dati: dict) -> str | None:
    cwd = radice(dati)
    pezzi: list[str] = []

    testo = leggi(cwd / "context.md")
    if testo is None:
        pezzi.append(
            "[context.md non trovato nella radice del progetto — verifica il percorso]"
        )
    else:
        righe = righe_di(testo)
        caratteri = len(testo)
        if caratteri > LIMITE_CARATTERI:
            testo = (
                testo[:LIMITE_CARATTERI]
                + "\n\n[troncato — context.md supera il limite di iniezione, "
                "aprilo con i tuoi strumenti per il resto]"
            )
        pezzi.append(testo)
        if caratteri > SOGLIA_AVVISO_CARATTERI:
            pezzi.append(
                f"\n> context.md occupa {caratteri} caratteri su {righe} righe, "
                f"oltre l'{SOGLIA_AVVISO_CARATTERI * 100 // LIMITE_CARATTERI}% del "
                f"budget di iniezione ({LIMITE_CARATTERI}). Oltre il tetto viene "
                "troncato e le voci in fondo spariscono da qui: chiudi o sposta "
                "qualche voce prima di aggiungerne altre."
            )

    testo_bug = leggi(cwd / "bug.md")
    if testo_bug is not None:
        pezzi.append(
            f"\n(bug.md esiste, {righe_di(testo_bug)} righe — non iniettato per "
            "intero qui: cercalo con grep quando un sintomo sembra già visto, "
            "oppure scrivi un prompt che lo nomini, così l'hook su "
            "UserPromptSubmit lo aggancia da solo.)"
        )

    testo_claude = leggi(cwd / "CLAUDE.md")
    if testo_claude is not None:
        righe_claude = righe_di(testo_claude)
        if righe_claude > SOGLIA_RIGHE_CLAUDE:
            pezzi.append(
                f"\n> CLAUDE.md ha {righe_claude} righe — sopra la soglia "
                f"(~{SOGLIA_RIGHE_CLAUDE}) oltre la quale l'aderenza alle "
                "istruzioni cala. Taglia prima di aggiungere."
            )

    return "\n".join(pezzi) if pezzi else None


def main() -> None:
    try:
        dati = json.load(sys.stdin)
        if not isinstance(dati, dict):
            return
        contesto = componi(dati)
    except Exception:  # noqa: BLE001 — un hook di avvio non solleva mai
        return

    if not contesto:
        return

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": contesto,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
