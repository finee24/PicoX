#!/usr/bin/env pwsh
# Avvia il backend Picox in sviluppo.
#
# Esiste perche' i due valori che contano sono esattamente quelli che si
# sbagliano riscrivendoli a mano ogni volta:
#
#   * l'interprete — il Python di sistema non ha le dipendenze del progetto, e
#     un avvio che "parte" con l'interprete sbagliato fallisce piu' tardi e in
#     un punto che non somiglia alla causa;
#   * la porta — 8000 e' il default di uvicorn, ma il frontend cerca il backend
#     sulla 8001 (`NEXT_PUBLIC_BACKEND_URL` in `frontend/.env.local`). Sbagliarla
#     non da' un errore di avvio: da' "Failed to fetch" dall'altra parte.

$ErrorActionPreference = 'Stop'

# Il percorso si ricava dallo script e non dalla cartella corrente, cosi' lo si
# puo' invocare da qualunque punto del repository.
$backend = $PSScriptRoot
$python = Join-Path $backend '.venv\Scripts\python.exe'

# Si controlla l'INTERPRETE, non la cartella `.venv`: un virtualenv interrotto a
# meta' lascia la cartella senza l'eseguibile, e un Test-Path sulla sola
# directory lo darebbe per buono.
if (-not (Test-Path $python)) {
    # `[Console]::Error` invece di Write-Error: quest'ultimo decora il messaggio
    # con il proprio blocco d'errore, e qui il messaggio *e'* il contenuto.
    [Console]::Error.WriteLine(@"

Virtualenv assente o incompleto: non trovo
  $python

Crealo cosi', da $backend :

  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt

Poi rilancia questo script.

Nessun fallback al Python di sistema, di proposito: senza il virtualenv le
dipendenze non ci sono, e un avvio che sembra riuscito con l'interprete
sbagliato costa piu' di un errore subito.

"@)
    exit 1
}

# uvicorn importa `app.main` come modulo e `--reload` sorveglia la cartella
# corrente: entrambi vogliono `backend/` come working directory, non quella da
# cui e' partito il comando.
Push-Location $backend
try {
    & $python -m uvicorn app.main:app --reload --port 8001
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
