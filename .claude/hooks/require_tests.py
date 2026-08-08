#!/usr/bin/env python
"""Stop — non si chiude il turno con i test del backend rossi.

Il caso che questo hook impedisce: una modifica al backend, un riepilogo che
dice "fatto", e la scoperta del guasto al push successivo. Il momento giusto per
scoprirlo è mentre il contesto della modifica è ancora aperto.

Si attiva **solo** se ci sono modifiche non committate sotto `backend/`: un
turno che ha toccato solo il frontend o la documentazione non paga il costo
della suite.

Sulla protezione dal ciclo infinito. Se i test non passano, il modello riprova;
se continuano a non passare, bloccare ancora produrrebbe un ciclo senza uscita.
Il campo `stop_hook_active` vale `True` quando il turno prosegue proprio perché
questo hook l'ha già bloccato: in quel caso si lascia terminare. La suite resta
rossa, ma è una condizione visibile e discutibile, non una sessione bloccata.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Vedi block_frontend_secrets.py: la console Windows non usa UTF-8 di default.
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

TIMEOUT_PYTEST = 300
# Quante righe dell'output di pytest riportare: l'intero output di una suite
# rotta può essere lunghissimo, e le ultime righe contengono il riepilogo.
RIGHE_RIPORTATE = 40


def _radice_repo(partenza: Path) -> Path | None:
    for candidato in [partenza, *partenza.parents]:
        if (candidato / ".git").exists():
            return candidato
    return None


def _python_del_venv(backend: Path) -> str | None:
    for candidato in ("Scripts/python.exe", "bin/python"):
        percorso = backend / ".venv" / candidato
        if percorso.exists():
            return str(percorso)
    return shutil.which("python") or shutil.which("python3")


def _backend_modificato(repo: Path) -> bool:
    """Ci sono modifiche non committate sotto `backend/`?"""
    try:
        risultato = subprocess.run(
            ["git", "status", "--porcelain", "--", "backend"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return risultato.returncode == 0 and bool(risultato.stdout.strip())


def main() -> int:
    try:
        evento = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    # Il turno è già stato bloccato una volta: non si insiste.
    if evento.get("stop_hook_active"):
        return 0

    cwd = Path(evento.get("cwd") or Path.cwd())
    repo = _radice_repo(cwd.resolve())
    if repo is None:
        return 0

    backend = repo / "backend"
    if not (backend / "pyproject.toml").exists() or not (backend / "tests").exists():
        return 0

    if not _backend_modificato(repo):
        return 0

    python = _python_del_venv(backend)
    if python is None:
        return 0

    try:
        risultato = subprocess.run(
            [python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=backend,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_PYTEST,
            check=False,
            # Senza, l'output torna al modello pieno di sequenze ANSI.
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except subprocess.TimeoutExpired:
        print(
            f"I test del backend non sono terminati entro {TIMEOUT_PYTEST}s. "
            "Probabile attesa su una risorsa esterna: la suite è progettata per "
            "non toccare la rete, quindi vale la pena capire cosa la blocca.",
            file=sys.stderr,
        )
        return 2
    except OSError:
        return 0

    # 0 = tutto verde. 5 = nessun test raccolto, che non è un fallimento del
    # codice in esame.
    if risultato.returncode in (0, 5):
        return 0

    uscita = (risultato.stdout or "") + (risultato.stderr or "")
    righe = uscita.strip().splitlines()
    coda = "\n".join(righe[-RIGHE_RIPORTATE:])

    print(
        "I test del backend non passano: il turno non può concludersi.\n\n"
        f"{coda}\n\n"
        "Riproducibile con:  cd backend && .venv/Scripts/python -m pytest\n"
        "Se il comportamento nuovo è quello corretto, va aggiornato il test; "
        "altrimenti va corretto il codice.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
