#!/usr/bin/env python
"""Verifica delle variabili d'ambiente, eseguita prima di avviare il server.

Senza questo controllo una configurazione incompleta si manifesta tardi e male:
il container parte, risponde `200` su `/health` — che non tocca né database né
servizi esterni — e fallisce solo alla prima richiesta reale, con un `401` o un
`503` che non dicono quale variabile manchi.

I casi elencati qui sotto non sono ipotetici: sono i modi in cui questo progetto
si è già rotto. In particolare `SUPABASE_JWT_SECRET=` (valorizzata a stringa
vuota invece che assente) manda il backend sul percorso di verifica HS256 in un
progetto che firma in ES256, e il risultato è che *ogni* richiesta autenticata
risponde 401 — con la variabile che "sembra" configurata correttamente.

Nessun valore viene stampato: il report dice quale variabile è a posto e quale
no, mai cosa contiene. Un log di avvio finisce nella dashboard della piattaforma
e spesso in un aggregatore di terze parti.

Uso:
    python scripts/check_env.py           # verifica e basta
    python scripts/check_env.py --strict  # anche i warning diventano bloccanti
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# Il report usa `✓`, `✗` e `!`. Su una console Windows a cp1252 la `print` di un
# carattere fuori dalla codepage solleva `UnicodeEncodeError`, e lo script
# moriva **proprio quando aveva un errore da segnalare** — il caso peggiore per
# uno strumento diagnostico, perché l'unico output che si vede è un traceback
# sull'encoding invece della variabile mancante.
#
# `block_frontend_secrets.py` risolve lo stesso problema riconfigurando
# `stderr`; qui il report esce da `print`, quindi il flusso da riconfigurare è
# **stdout**. Entrambi, per sicurezza. `errors="replace"` è la rete finale: se
# anche utf-8 non fosse scrivibile, si perde un glifo e non il messaggio.
for _flusso in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _flusso.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

# `Settings` legge `("../.env", ".env")` relativi alla CWD, ma questo script
# guardava solo `os.environ`: eseguito come documenta il README — fuori da
# Docker, dalla cartella `backend/` — riportava *tutte* le variabili
# obbligatorie come assenti, pur essendo la configurazione corretta. Dentro
# Docker funzionava, perché `env_file` popola l'ambiente prima dell'entrypoint,
# ed è per questo che il difetto era rimasto invisibile.
#
# L'ORDINE DI CARICAMENTO NON È CASUALE. `load_dotenv` con `override=False`
# lascia vincere ciò che è già in `os.environ`: quindi il **primo** file
# caricato vince sul secondo, e le variabili d'ambiente vere vincono su
# entrambi. Per riprodurre la precedenza dichiarata in `config.py` — «`backend/.env`
# ha precedenza sul `.env` di root» — `backend/.env` va caricato per primo.
_BACKEND = Path(__file__).resolve().parent.parent
for _env in (_BACKEND / ".env", _BACKEND.parent / ".env"):
    if _env.is_file():
        load_dotenv(_env, override=False)

# --- Obbligatorie: senza, il backend non può funzionare ----------------------
REQUIRED: dict[str, str] = {
    "SUPABASE_URL": "URL del progetto Supabase (https://<ref>.supabase.co)",
    "SUPABASE_ANON_KEY": "Chiave pubblica, usata per il client con RLS attivo",
    "SUPABASE_SERVICE_ROLE_KEY": "Chiave server-side che bypassa il RLS",
    "GEMINI_API_KEY": "Chiave dell'API Gemini",
    "APIFY_API_TOKEN": "Token Apify per lo scraping",
    "CRON_SECRET": "Segreto condiviso dell'header X-CRON-SECRET",
}

# --- Facoltative ma con un default che raramente è quello giusto -------------
RECOMMENDED: dict[str, str] = {
    "FRONTEND_URL": "Unico origin ammesso dal CORS (default http://localhost:3000)",
    "ENVIRONMENT": "development | production (default development)",
}

# Valori copiati dal `.env.example` e mai compilati.
PLACEHOLDERS = frozenset(
    {
        "", "changeme", "your-key-here", "xxx", "todo", "<inserisci>",
        "your-api-key", "sk-...", "...",
    }
)

VERDE, ROSSO, GIALLO, GRIGIO, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"


def _colore(testo: str, colore: str) -> str:
    # Niente sequenze ANSI quando l'output non è un terminale: nei log di Render
    # o di GitHub Actions diventerebbero caratteri di disturbo.
    return f"{colore}{testo}{RESET}" if sys.stdout.isatty() else testo


class Report:
    def __init__(self) -> None:
        self.errori: list[str] = []
        self.avvisi: list[str] = []

    def errore(self, messaggio: str) -> None:
        self.errori.append(messaggio)
        print(f"  {_colore('✗', ROSSO)} {messaggio}")

    def avviso(self, messaggio: str) -> None:
        self.avvisi.append(messaggio)
        print(f"  {_colore('!', GIALLO)} {messaggio}")

    def ok(self, messaggio: str) -> None:
        print(f"  {_colore('✓', VERDE)} {messaggio}")


def controlla_obbligatorie(report: Report) -> None:
    print("\nVariabili obbligatorie")
    for nome, descrizione in REQUIRED.items():
        valore = os.environ.get(nome)
        if valore is None:
            report.errore(f"{nome} assente — {descrizione}")
        elif valore.strip().lower() in PLACEHOLDERS:
            report.errore(f"{nome} contiene un segnaposto non compilato — {descrizione}")
        else:
            report.ok(f"{nome} ({len(valore)} caratteri)")


def controlla_consigliate(report: Report) -> None:
    print("\nVariabili consigliate")
    for nome, descrizione in RECOMMENDED.items():
        valore = os.environ.get(nome)
        if not valore:
            report.avviso(f"{nome} non impostata, si userà il default — {descrizione}")
        else:
            report.ok(f"{nome}={valore}")


def controlla_insidie(report: Report) -> None:
    """I modi specifici in cui questa configurazione si è già rotta."""
    print("\nControlli specifici")

    # 1. Stringa vuota != assente. pydantic-settings legge `SUPABASE_JWT_SECRET=`
    #    come SecretStr("") — che *non* è None — e sceglie la verifica HS256.
    if "SUPABASE_JWT_SECRET" in os.environ:
        if os.environ["SUPABASE_JWT_SECRET"].strip() == "":
            report.errore(
                "SUPABASE_JWT_SECRET è presente ma vuota. Una riga vuota non equivale "
                "all'assenza: il backend userebbe la verifica HS256 e ogni richiesta "
                "autenticata risponderebbe 401. Rimuovere del tutto la riga per usare "
                "le chiavi asimmetriche (JWKS)."
            )
        else:
            report.ok("SUPABASE_JWT_SECRET impostata (verifica HS256)")
    else:
        report.ok("SUPABASE_JWT_SECRET assente (verifica via JWKS, chiavi asimmetriche)")

    # 2. Le due chiavi Supabase confuse fra loro.
    anon = os.environ.get("SUPABASE_ANON_KEY")
    service = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if anon and service and anon == service:
        report.errore(
            "SUPABASE_ANON_KEY e SUPABASE_SERVICE_ROLE_KEY hanno lo stesso valore: "
            "una delle due è sbagliata."
        )

    # 3. URL Supabase malformato.
    url = os.environ.get("SUPABASE_URL", "")
    if url:
        parti = urlsplit(url)
        if parti.scheme != "https":
            report.errore(f"SUPABASE_URL non usa https (schema: {parti.scheme or 'assente'})")
        elif not parti.netloc:
            report.errore("SUPABASE_URL non contiene un host valido")
        elif parti.path.rstrip("/"):
            report.avviso("SUPABASE_URL contiene un path: verrà usato solo l'host")
        else:
            report.ok("SUPABASE_URL ben formato")

    # 4. CORS: FRONTEND_URL è un origin, non un URL con path.
    frontend = os.environ.get("FRONTEND_URL", "")
    if frontend:
        parti = urlsplit(frontend)
        if not parti.scheme or not parti.netloc:
            report.errore(f"FRONTEND_URL non è un origin valido: serve schema + host ({frontend})")
        elif parti.path.rstrip("/"):
            report.errore(
                "FRONTEND_URL contiene un path: il CORS confronta gli origin, "
                "quindi il browser non riconoscerebbe l'origin come ammesso."
            )
        else:
            report.ok(f"FRONTEND_URL è un origin valido ({frontend})")

    # 5. Segreto del cron troppo corto per resistere a un tentativo a forza bruta.
    cron = os.environ.get("CRON_SECRET", "")
    if cron and len(cron) < 24:
        report.avviso(
            f"CRON_SECRET è lungo {len(cron)} caratteri: consigliati almeno 24 "
            "(es. `openssl rand -hex 32`)."
        )

    # 6. In produzione i segreti da sviluppo non devono sopravvivere.
    if os.environ.get("ENVIRONMENT", "").lower() in {"production", "prod"}:
        if "localhost" in frontend or "127.0.0.1" in frontend:
            report.errore(f"ENVIRONMENT=production ma FRONTEND_URL punta in locale ({frontend})")
        if "dev" in cron.lower() or "test" in cron.lower():
            report.errore("ENVIRONMENT=production ma CRON_SECRET sembra un segreto di sviluppo")


def controlla_caricamento_settings(report: Report) -> None:
    """Ultimo passo: le impostazioni si costruiscono davvero?

    I controlli qui sopra guardano l'ambiente grezzo; questo verifica che
    pydantic accetti l'insieme — validatori compresi.
    """
    print("\nCaricamento della configurazione")
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.core.config import Settings

        impostazioni = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001 — qualunque errore qui è un blocco
        # `type(exc).__name__` e non `str(exc)`: il messaggio di pydantic può
        # riportare i valori dei campi che hanno fallito la validazione.
        report.errore(f"Settings non costruibile: {type(exc).__name__}")
        return

    report.ok(f"Configurazione valida (environment={impostazioni.environment})")
    report.ok(f"Origin CORS ammessi: {', '.join(impostazioni.cors_origins) or 'nessuno'}")
    report.ok(f"Modello Gemini: {impostazioni.gemini_model}")


def main() -> int:
    strict = "--strict" in sys.argv

    print(_colore("Picox — verifica della configurazione", GRIGIO))

    report = Report()
    controlla_obbligatorie(report)
    controlla_consigliate(report)
    controlla_insidie(report)
    if not report.errori:
        controlla_caricamento_settings(report)

    print()
    if report.errori:
        print(_colore(f"{len(report.errori)} errore/i: il backend non verrà avviato.", ROSSO))
        return 1
    if report.avvisi and strict:
        print(_colore(f"{len(report.avvisi)} avviso/i con --strict attivo.", ROSSO))
        return 1
    if report.avvisi:
        print(_colore(f"Configurazione utilizzabile, {len(report.avvisi)} avviso/i.", GIALLO))
    else:
        print(_colore("Configurazione completa.", VERDE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
