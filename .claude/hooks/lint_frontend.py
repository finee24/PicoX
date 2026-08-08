#!/usr/bin/env python
"""PostToolUse — `eslint --fix` dopo ogni modifica a un `.ts`/`.tsx`.

`--fix` applica da sé quello che è meccanico (ordine degli import, virgole,
spaziature) e riporta al modello solo ciò che richiede una decisione. Questo
progetto usa `eslint-config-next`, che intercetta anche errori sostanziali —
`setState` dentro un effect, navigazioni con `window.location`, hook chiamati
condizionalmente — cioè bug, non preferenze di formattazione.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Vedi block_frontend_secrets.py: la console Windows non usa UTF-8 di default.
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

TIMEOUT = 90
ESTENSIONI = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def main() -> int:
    try:
        evento = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    percorso = (evento.get("tool_input") or {}).get("file_path")
    if not isinstance(percorso, str) or not percorso.endswith(ESTENSIONI):
        return 0

    file_modificato = Path(percorso)
    if not file_modificato.exists():
        return 0

    # Radice del frontend: la cartella con `package.json`.
    frontend = next(
        (
            genitore
            for genitore in file_modificato.resolve().parents
            if (genitore / "package.json").exists()
        ),
        None,
    )
    if frontend is None or not (frontend / "node_modules").exists():
        # Senza dipendenze installate non c'è nulla da eseguire: scaricare eslint
        # dalla rete a metà di un turno sarebbe peggio del non far niente.
        return 0

    # Si invoca l'entrypoint JS di eslint con `node`, non `npx`.
    #
    # `npx` su Windows è uno script `.cmd`, che CreateProcess non sa eseguire:
    # da lì nasceva `shell=True`, e con esso un'esecuzione di comandi arbitraria.
    # `subprocess.list2cmdline` cita gli argomenti per il parser del C runtime,
    # non per `cmd.exe`: un file chiamato `x&calc.ts` passa il `&` intatto, e
    # `cmd.exe` lo interpreta come separatore di comandi. Questo hook scatta su
    # ogni Write/Edit e ha accesso a `backend/.env`, quindi il nome di un file
    # creato dall'agente non deve poter diventare un comando.
    #
    # `node` è un eseguibile vero: niente `.cmd`, niente shell, niente parsing.
    eslint = frontend / "node_modules" / "eslint" / "bin" / "eslint.js"
    if not eslint.exists():
        return 0

    try:
        risultato = subprocess.run(
            ["node", str(eslint), "--fix", str(file_modificato)],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0

    if risultato.returncode == 0:
        return 0

    uscita = (risultato.stdout or risultato.stderr or "").strip()
    if not uscita:
        return 0

    print(
        f"ESLint segnala problemi non correggibili automaticamente in "
        f"{file_modificato.name}:\n\n{uscita}\n\n"
        "Le regole di `eslint-config-next` che restano dopo `--fix` sono quasi "
        "sempre errori di comportamento, non di stile.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
