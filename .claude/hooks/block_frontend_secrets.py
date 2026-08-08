#!/usr/bin/env python
"""PreToolUse — impedisce che un segreto server-only entri in `frontend/`.

Perché un hook e non la sola revisione: tutto ciò che sta in `frontend/` viene
compilato in un bundle e servito al browser. `GEMINI_API_KEY` o
`SUPABASE_SERVICE_ROLE_KEY` scritte lì non sono un errore da correggere in
review — sono una credenziale già distribuita a chiunque apra il sito, da
revocare e rigenerare. Un controllo deterministico prima della scrittura è
l'unico che non dipende da chi legge la diff.

Protocollo: JSON su stdin, uscita **2** per bloccare la chiamata; il testo su
stderr torna al modello, che può correggersi da solo.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Su Windows lo stderr eredita il codepage della console (spesso cp1252), che
# non copre gli accenti di questi messaggi: senza questa riga l'output arriva
# a Claude Code corrotto, o solleva UnicodeEncodeError proprio mentre si sta
# cercando di segnalare un problema di sicurezza.
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

# Nomi che non devono comparire nel frontend, in nessuna forma.
SEGRETI_SERVER = (
    "GEMINI_API_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
)

# Anche il *valore*, non solo il nome: incollare la chiave senza mai nominarla
# aggirerebbe un controllo basato sui soli identificatori.
VALORI = {
    "token Apify": re.compile(r"apify_api_[A-Za-z0-9]{25,}"),
    "chiave Google AIza": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "chiave Google AQ.": re.compile(r"AQ\.[A-Za-z0-9_-]{30,}"),
    # I tre allineamenti base64 di `service_role` dentro il payload di un JWT
    # Supabase: la chiave anon, che è pubblica per progettazione, non matcha.
    "chiave service_role Supabase (legacy)": re.compile(
        r"ZXJ2aWNlX3Jv|c2VydmljZV9y|cnZpY2Vfcm9s"  # scan-secrets:ok — è il pattern, non una chiave
    ),
    # Il formato nuovo è opaco, non un JWT: nessun frammento base64 lo
    # intercetta. `sb_publishable_...` è esclusa di proposito — è pubblica e nel
    # frontend ci deve stare.
    "secret key Supabase (sb_secret_)": re.compile(r"sb_secret_[A-Za-z0-9_-]{20,}"),
}

# `.env.local.example` documenta i nomi delle variabili pubbliche: è il posto in
# cui devono comparire, non un'eccezione a cui si ricorre per aggirare l'hook.
FILE_ESENTI = {".env.local.example", "VERCEL.md"}


def _testo_scritto(tool_input: dict) -> str:
    """Il contenuto che la chiamata sta per scrivere.

    `Write` porta tutto in `content`; `Edit` solo il frammento nuovo. In
    entrambi i casi ciò che conta è quello che finirà sul disco.
    """
    parti = [
        tool_input.get("content"),
        tool_input.get("new_string"),
    ]
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        parti.extend(
            edit.get("new_string") for edit in edits if isinstance(edit, dict)
        )
    return "\n".join(part for part in parti if isinstance(part, str))


def main() -> int:
    try:
        evento = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Senza un payload leggibile non si può decidere: non si blocca, ma lo
        # si segnala. Un hook che rifiuta tutto per un errore di parsing viene
        # disattivato entro un'ora, e a quel punto non protegge più niente.
        print("Hook segreti: payload non leggibile, controllo saltato.", file=sys.stderr)
        return 0

    tool_input = evento.get("tool_input") or {}
    percorso = tool_input.get("file_path")
    if not isinstance(percorso, str) or not percorso:
        return 0

    path = Path(percorso)
    # `parts` e non una ricerca nella stringa: `/frontend` come sottostringa
    # matcherebbe anche `backend/my-frontend-notes.md`.
    if "frontend" not in path.parts:
        return 0
    if path.name in FILE_ESENTI:
        return 0

    contenuto = _testo_scritto(tool_input)
    if not contenuto:
        return 0

    trovati = [nome for nome in SEGRETI_SERVER if nome in contenuto]
    trovati += [
        etichetta for etichetta, pattern in VALORI.items() if pattern.search(contenuto)
    ]

    if not trovati:
        return 0

    print(
        f"BLOCCATO: scrittura in {percorso} contenente segreti server-only "
        f"({', '.join(trovati)}).\n"
        "\n"
        "Tutto ciò che sta in frontend/ viene servito al browser: questi valori "
        "sarebbero leggibili da chiunque apra il sito, anche se non compaiono a "
        "schermo.\n"
        "\n"
        "Come procedere:\n"
        "  - la chiamata a Gemini o Apify va fatta dal backend, che espone il "
        "risultato tramite un endpoint autenticato;\n"
        "  - nel frontend sono ammesse solo NEXT_PUBLIC_SUPABASE_URL, "
        "NEXT_PUBLIC_SUPABASE_ANON_KEY e NEXT_PUBLIC_BACKEND_URL;\n"
        "  - se il valore è finito in un file anche solo temporaneamente, va "
        "considerato compromesso e rigenerato.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
