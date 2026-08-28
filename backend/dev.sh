#!/usr/bin/env bash
# Avvia il backend Picox in sviluppo. Equivalente di `dev.ps1` per bash/zsh.
#
# L'uso quotidiano e' su Windows, ma i due valori che questo script fissa —
# l'interprete del virtualenv e la porta 8001 — valgono uguali ovunque, e il
# README non deve avere due comandi diversi a seconda della shell.
set -euo pipefail

# Il percorso si ricava dallo script e non dalla cartella corrente, cosi' lo si
# puo' invocare da qualunque punto del repository.
backend="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Si cerca l'INTERPRETE, non la cartella `.venv`: un virtualenv interrotto a
# meta' lascia la cartella senza l'eseguibile. I due percorsi sono quelli dei
# due sistemi — `bin/` su Unix, `Scripts/` su Windows — perche' lo stesso
# repository viene aperto da entrambi.
if [ -x "$backend/.venv/bin/python" ]; then
  python="$backend/.venv/bin/python"
elif [ -x "$backend/.venv/Scripts/python.exe" ]; then
  python="$backend/.venv/Scripts/python.exe"
else
  cat >&2 <<EOF

Virtualenv assente o incompleto: non trovo ne'
  $backend/.venv/bin/python
ne'
  $backend/.venv/Scripts/python.exe

Crealo cosi', da $backend :

  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt

Poi rilancia questo script.

Nessun fallback al Python di sistema, di proposito: senza il virtualenv le
dipendenze non ci sono, e un avvio che sembra riuscito con l'interprete
sbagliato costa piu' di un errore subito.

EOF
  exit 1
fi

# uvicorn importa app.main come modulo e --reload sorveglia la cartella
# corrente: entrambi vogliono backend/ come working directory.
cd "$backend"

# exec, e non una chiamata normale: cosi' uvicorn prende il posto della shell
# e riceve Ctrl-C direttamente, invece di lasciarlo intercettare a un padre che
# poi deve ricordarsi di propagarlo.
exec "$python" -m uvicorn app.main:app --reload --port 8001
