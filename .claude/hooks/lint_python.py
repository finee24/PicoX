#!/usr/bin/env python
"""PostToolUse — `ruff` e `mypy` dopo ogni modifica a un file `.py` del backend.

L'errore torna al modello subito dopo la scrittura, quando il contesto di
quella modifica è ancora presente: correggerlo costa una riga. Scoprirlo in CI
mezz'ora dopo costa ricostruire tutto il ragionamento.

Regole e versioni vengono da `backend/pyproject.toml` e
`backend/requirements-dev.txt`, gli stessi che usa la CI: un lint verde qui e
rosso in GitHub Actions sarebbe peggio che non averlo.
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

# mypy su progetto freddo può superare il minuto; a cache calda sta in pochi
# secondi. Oltre questa soglia si rinuncia al risultato invece di far attendere
# il turno: ci pensa comunque la CI, dove il tempo non è interattivo.
TIMEOUT_RUFF = 60
TIMEOUT_MYPY = 90


def _python_del_venv(backend: Path) -> str | None:
    """Interprete del virtualenv del backend, se c'è.

    Va usato quello: `ruff` e `mypy` installati lì hanno le versioni pinnate e
    vedono le dipendenze del progetto. Un `mypy` di sistema segnalerebbe come
    mancanti tutti gli import di fastapi e pydantic.
    """
    for candidato in ("Scripts/python.exe", "bin/python"):
        percorso = backend / ".venv" / candidato
        if percorso.exists():
            return str(percorso)
    return shutil.which("python") or shutil.which("python3")


def _esegui(python: str, argomenti: list[str], cwd: Path, timeout: int):
    # NO_COLOR: senza, l'output arriva pieno di sequenze ANSI che nel messaggio
    # restituito al modello sono solo rumore.
    ambiente = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    try:
        return subprocess.run(
            [python, "-m", *argomenti],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=ambiente,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None


def main() -> int:
    try:
        evento = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    percorso = (evento.get("tool_input") or {}).get("file_path")
    if not isinstance(percorso, str) or not percorso.endswith(".py"):
        return 0

    file_modificato = Path(percorso)
    if not file_modificato.exists():  # es. il file è stato rinominato o rimosso
        return 0

    # Radice del backend: la cartella che contiene `pyproject.toml`.
    backend = next(
        (
            genitore
            for genitore in file_modificato.resolve().parents
            if (genitore / "pyproject.toml").exists()
        ),
        None,
    )
    if backend is None:
        return 0

    python = _python_del_venv(backend)
    if python is None:
        return 0

    problemi: list[str] = []

    # Ruff sul solo file toccato: è veloce e il risultato è mirato.
    ruff = _esegui(
        python,
        ["ruff", "check", "--output-format=concise", str(file_modificato)],
        backend,
        TIMEOUT_RUFF,
    )
    if ruff is not None and ruff.returncode != 0:
        problemi.append(f"ruff:\n{(ruff.stdout or ruff.stderr).strip()}")

    # Mypy sull'intero progetto: su un file isolato non vedrebbe i tipi degli
    # altri moduli e produrrebbe errori inventati. Vengono però riportati solo
    # i messaggi che riguardano il file appena modificato — il resto è rumore
    # rispetto alla modifica in corso.
    mypy = _esegui(python, ["mypy"], backend, TIMEOUT_MYPY)
    if mypy is not None and mypy.returncode != 0:
        try:
            relativo = str(file_modificato.resolve().relative_to(backend))
        except ValueError:
            relativo = file_modificato.name
        atteso = relativo.replace("\\", "/")
        righe = [
            riga
            for riga in (mypy.stdout or "").splitlines()
            if riga.replace("\\", "/").startswith(atteso)
        ]
        if righe:
            problemi.append("mypy:\n" + "\n".join(righe))

    if not problemi:
        return 0

    print(
        f"Controlli falliti su {file_modificato.name}:\n\n"
        + "\n\n".join(problemi)
        + "\n\nVanno corretti prima di proseguire: sono gli stessi controlli "
        "che la CI esegue su ogni push.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
