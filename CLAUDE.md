# CLAUDE.md — Picox

Direzione stabile per lavorare su Picox: regole e decisioni non ovvie da non
contraddire. Non lo stato dei task (vive in `context.md`) né il ragionamento
sui bug (vive in `bug.md`).

## Cos'è Picox

SaaS che analizza video brevi (Reels, TikTok, YouTube Shorts) con AI
multimodale (Gemini) ed estrae insight riutilizzabili: sintesi, analisi di
stile, script inverso. Monorepo: backend FastAPI, frontend Next.js 16 (PWA),
Supabase (Postgres + Auth + RLS). Scraping via Apify.

## Visione di prodotto

Non solo "far girare il codice": il servizio deve essere veloce, comodo,
massimamente funzionale — l'obiettivo è essere indispensabile per chi crea
contenuti social.

**Oggi (costruito):** l'utente aggiunge i creator che segue o da cui si
ispira, l'app li analizza ed estrae sintesi, stile, script inverso.

**Direzione dichiarata** (non ancora costruita, ma da tenere presente):
l'utente dà un'**idea** di contenuto, l'app sceglie i creator più rilevanti
(o li sceglie il cliente), analizza i loro video andati bene e male, spiega
**perché** — tecniche, minutaggio — e produce lo script inverso a partire da
quell'idea, non solo dal creator monitorato.

Un'implicazione già nello schema: `insights.creator_id` è nullable apposta
(vedi `ON DELETE SET NULL` sotto) — un insight può già esistere senza
appartenere a un creator della watchlist dell'utente. Non romperla per una
scorciatoia: è ciò che rende possibile, domani, l'analisi su creator esterni
alla watchlist di chi chiede.

**Essere lungimirante non significa costruire in anticipo ciò che non è
stato chiesto.** Significa: se una scelta rapida di oggi blocca chiaramente
questa direzione (es. un'assunzione hardcoded che un video appartenga sempre
a un creator dell'utente), dillo prima di procedere — non deciderlo in
silenzio, e non costruire da solo la generalizzazione che non è stata
richiesta.

## All'inizio di una sessione nuova

- Leggi `context.md` per lo stato attivo (task in corso, blocker) prima di
  proporre lavoro.
- Se il task riguarda un bug, un comportamento anomalo o una regressione,
  leggi `bug.md` prima di ipotizzare una causa — potrebbe essere già successo
  ed essere già diagnosticato.
- Questi due file non sono ancora agganciati con un hook automatico: se non
  compaiono nel contesto, aprili con i tuoi strumenti di lettura file.

## Strumenti già disponibili in `.claude/`

- **Agenti** (`.claude/agents/`): `security-reviewer.md` — invocalo per
  modifiche che toccano autenticazione, RLS, denaro/quota, o superficie
  esposta a Internet (nuovo endpoint pubblico, webhook), prima di considerare
  il lavoro finito. `prompt-tuner.md` — per modifiche ai prompt Gemini.
- **Hook** (`.claude/hooks/`): girano già in automatico, non serve invocarli.
  `block_frontend_secrets.py` blocca segreti scritti in `frontend/`;
  `lint_python.py`/`lint_frontend.py` fanno lint dopo ogni modifica;
  `require_tests.py` non chiude il turno se `pytest` fallisce su modifiche in
  `backend/`. Se un turno si chiude senza che siano scattati dove dovevano,
  verifica perché — vedi `.claude/hooks/README.md`.
- **Frontend** (`frontend/AGENTS.md`, caricato da `frontend/CLAUDE.md`):
  questo Next.js ha breaking changes rispetto a quanto un modello ricorda —
  leggi `node_modules/next/dist/docs/` prima di scrivere codice. Lo rigenera
  `next dev`: se ricompare in un diff non toglierlo, committalo.
- **Skill** (`.claude/skills/`): convenzioni HTTP/REST delle route in
  `api-conventions`; come strutturare prompt e `response_schema` per Gemini
  nella skill Gemini. Consultale prima di improvvisare una convenzione nuova.

## Convenzioni

- Codice, commenti, nomi dei test e messaggi d'errore di dominio: in
  italiano. I commenti spiegano il *perché*, non il *cosa*.
- Config di pytest, ruff, mypy unica in `backend/pyproject.toml`: hook
  locali e CI leggono le stesse regole.
- Test di autenticazione con un JWT HS256 vero, mai `dependency_overrides`
  su `get_current_user`: un override renderebbe verdi i test sui 401 per
  costruzione, senza verificare nulla.
- Il doppio di Supabase per i test (`fake_supabase.py`) applica lui stesso
  il RLS — una query scoped che perde il filtro sul tenant deve far fallire
  il test, non passarlo.
- Le route protette si enumerano dallo schema OpenAPI nei test, non da
  `app.routes` (questa versione di FastAPI non appiattisce i router
  inclusi: iterarlo darebbe un test verde che non verifica nulla).

## Architettura — regole non negoziabili

- **Due client Supabase, mai intercambiabili.** Richieste utente → anon key
  + JWT, RLS attivo. Service role (bypassa il RLS) solo dove non esiste un
  JWT — scrittura post-inferenza, cron — e sempre tramite
  `service_table(tabella, user_id)`, che impone da sé il filtro sul
  proprietario. Unica eccezione: `unscoped_service_table`, con allowlist
  esplicita (`creators`, `job_locks`, `creator_validations`).
- **L'identità viene sempre dal JWT verificato, mai dal body.** Schemi con
  `extra="forbid"`.
- **Lo stato del piano vive in `subscriptions.tier`, mai in `profiles`.**
  `profiles` ha già avuto una policy di UPDATE ampia sull'intera riga — una
  colonna sensibile al piano lì sarebbe a un solo `GRANT` largo di distanza
  dall'auto-promozione (è già successo, vedi `SECURITY_AUDIT.md` A1). I
  tetti per piano (`creator_limit_for_tier`,
  `analysis_limit_for_tier`, `validation_limit_for_tier`) vivono in funzioni
  SQL uniche, mai duplicati in più trigger o nel codice Python: un `CASE`
  duplicato diverge silenziosamente quando i piani cambiano.
- **La cache è su `cache_key`, non su `video_url`.** `cache_key` è
  l'identità del video, calcolata una volta sola dentro `perform_analysis`
  e mai passata come parametro esterno — due chiamanti che la calcolano
  diversamente riaprono la divergenza che doveva chiudere. `video_url`
  resta il valore mostrato e cliccabile all'utente. `analysis_locks` e la
  dedup del cron (`_filter_already_analyzed`) devono restare allineate
  sulla stessa chiave: se un solo punto usa ancora `video_url`, si ripaga
  la stessa analisi due volte.
- **L'upsert su `insights` omette `creator_id` quando è `None`, non lo
  imposta esplicitamente.** Una chiave assente nel payload lascia intatto
  il valore già in archivio; un `None` esplicito lo cancella.
- **Locking a TTL su riga (`analysis_locks`, `job_locks`), non
  `pg_advisory_lock`.** Il backend parla con Postgres solo via PostgREST su
  HTTP: non c'è una connessione di sessione da tenere aperta su cui un lock
  avanzato avrebbe senso.
- **`analysis_lock_ttl_seconds` è un vincolo di correttezza;
  `analysis_lock_wait_seconds` è solo UX.** Il primo deve superare il caso
  peggiore di un'analisi legittima (inclusi i retry di Gemini) — se scade
  prima, un'altra richiesta sottrae il lock a un'analisi ancora in corso.
  Non alzare i retry o i timeout di Gemini senza ricontrollare questo
  numero.
- **Middleware del request-context: ASGI puro, mai `BaseHTTPMiddleware`.**
  Romperebbe la propagazione delle `contextvars` verso i `BackgroundTasks`
  del cron.
- **Ogni router applicativo usa `route_class=SafeRoute`.** Non solo per
  sanificare l'errore (lo fa comunque la rete esterna) ma perché un
  endpoint fuori da questo schema risponde con gli stessi byte, ma senza
  header CORS — il browser mostra "errore di rete" invece dell'envelope.
- **Il confronto dell'Origin per il CORS è per uguaglianza esatta, mai
  `startswith`.** `http://localhost:3000.evil.example` supererebbe un
  confronto con prefisso.
- **Forma unica dell'errore, mai stack trace al client:**
  `{"error": {"code","message","details"?}}`. `request_id` non torna mai al
  client (requisito esplicito).
- **I limiti di piano (creator attivi, quota giornaliera di analisi e di
  validazioni) sono applicati a livello di trigger Postgres, non solo nel
  backend.** Un utente autenticato può scrivere direttamente via PostgREST
  con la propria chiave: un controllo solo applicativo è aggirabile.

## Ambiente

- Backend in ascolto su porta **8001**, non 8000: avvialo con `backend/dev.ps1`
  (o `dev.sh`), che la fissa insieme all'interprete del virtualenv.
- `GEMINI_MODEL=gemini-flash-latest` — `gemini-2.5-flash` risponde 404 per
  questa chiave API, anche se compare ancora in `models.list()`.
- Per installare pacchetti nel frontend serve **Node 22 / npm 10** (`nvm
  use` legge `.nvmrc`). Altre versioni scrivono un lockfile che si rompe
  solo in CI; `engine-strict=true` in `.npmrc` blocca subito con
  `EBADENGINE`.
- Il limite di durata del video (`MAX_VIDEO_DURATION_SECONDS`) dipende da
  `ffprobe`: senza (Windows senza ffmpeg, o un runtime che non lo include)
  resta attivo solo il limite di dimensione, applicato durante lo
  streaming.
- `backend/.env` e `frontend/.env.local` esistono, sono gitignored: non
  leggerli ad alta voce, non incollarne il contenuto in chat, non
  committarli.
- Se in futuro si configura un SMTP proprio su Supabase Auth, si **rimuove
  una protezione silenziosa**: oggi la creazione di massa di account è
  limitata di fatto dalla quota dell'SMTP condiviso di Supabase (`HTTP 429
  over_email_send_rate_limit`), non da un controllo di sicurezza dedicato.
  Vedi voce A4 in `SECURITY_AUDIT.md` prima di farlo.

## Quando arriverà il billing (Stripe)

Non ancora costruito — nessun endpoint webhook, nessuna colonna oltre a
`subscriptions.tier`. Quattro trappole già mappate in `SECURITY_AUDIT.md`
(Categoria B), da non re-imparare a caro prezzo:

- **Firma del webhook sul corpo grezzo**, prima di qualunque parsing JSON —
  se FastAPI parsa e poi si riserializza per verificare, i byte cambiano e
  la firma non torna mai. Il pattern esiste già: `verify_cron_secret` usa
  `hmac.compare_digest` sui byte, riusalo. Verificare anche la tolleranza
  temporale sul timestamp della firma.
- **Le consegne duplicate sono la norma**, non l'anomalia (Stripe ritenta
  fino a 3 giorni). Serve una tabella dedicata, senza TTL:
  `processed_webhook_events (event_id text primary key, processed_at
  timestamptz)` — un evento già visto fa collidere l'`INSERT` e si scarta.
  Il principio (l'arbitro è un vincolo del database, non il processo) è
  già in uso in `analysis_locks`/`job_locks`, ma lì il meccanismo è un lock
  a TTL, non un inserimento permanente — stessa idea di fondo, meccanismo
  diverso: non copiare quel codice, copiare il principio.
- **Stripe non garantisce l'ordine di consegna**: un evento vecchio arrivato
  dopo uno nuovo non va applicato ciecamente, va confrontato il timestamp
  dell'oggetto.
- **Lo stato "pagato" non può restare una stringa senza scadenza**: serve
  `status` + fine periodo, non solo `tier`.

## Prima di considerare un task finito

- Backend: `pytest`, `ruff check`, `mypy` puliti.
- Frontend: `eslint`, `tsc --noEmit`, `next build` puliti.
- Per modifiche che toccano autenticazione, RLS, denaro/quota, o superficie
  esposta a Internet: valuta un giro di `security-reviewer` prima di dire
  che il lavoro è finito.
- **Se hai appena chiuso un bug che ha richiesto più di un'ipotesi prima
  della causa vera, aggiungi una voce a `bug.md` ora** — è il momento in cui
  costa meno, molto meno che ricostruirlo da documenti sparsi mesi dopo.
- **Se hai aperto, avanzato o chiuso un task rilevante, aggiorna
  `context.md` di conseguenza** — non lasciarlo indietro rispetto al lavoro
  reale: è esattamente il tipo di silenzio che ha reso `PROGRESS.md`
  inaffidabile.
- Mai invocare l'endpoint cron reale (`/api/v1/cron/check-updates`) contro
  il database di produzione per un test manuale: il suo perimetro sono
  tutti gli utenti attivi di Picox, non solo chi fa la richiesta.
