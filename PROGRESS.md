# PROGRESS — Picox

> Stato al **8 agosto 2026**. Documento di handoff: scritto per una sessione
> nuova, senza memoria della conversazione precedente. Dovrebbe bastare da solo.

---

## Obiettivo generale

Picox è un SaaS che analizza video brevi (Reels, TikTok, YouTube Shorts) con AI
multimodale (Gemini) e ne estrae insight riutilizzabili: sintesi dei contenuti,
analisi dello stile e "script inverso" riadattabile. Lo stack è un monorepo con
backend FastAPI, frontend Next.js 16 e Supabase (Postgres + Auth + RLS); lo
scraping dei video passa da Apify.

Backend, frontend e schema del database erano **già scritti e funzionanti in
locale** prima di questa sessione. Il lavoro di questa sessione è la fase di
**integrazione e messa in produzione**: audit dei contratti fra frontend e
backend, suite di test, hardening, CI, file di deploy (Render + Vercel),
scheduling del cron e documentazione.

---

## Task completate

### 1. Audit dei contratti frontend ↔ backend ✅

Confronto fra `frontend/lib/api.ts` + `frontend/lib/types.ts` e le route in
`backend/app/api/v1/`. **Nessuna divergenza bloccante trovata**: path, nomi dei
campi, tipi e status code combaciano, incluso `mode=SCRIPT` e la distinzione
200 (cache) / 201 (analisi nuova).

L'audit non è rimasto un documento: è stato codificato in
`backend/tests/test_api_contract.py`, che fissa i set di chiavi esatti delle
risposte (`CAMPI_INSIGHT`, `CAMPI_CREATOR`, `CAMPI_PAGINA`) contro le interfacce
di `frontend/lib/types.ts`. Se i due lati divergono, la CI se ne accorge.

**`ON DELETE SET NULL` su `insights.creator_id` — verificato su due livelli:**
- sul database reale, con una query a `information_schema` sul progetto Supabase
  `jaimkiagtolxbkftjapx`: il vincolo `insights_creator_id_fkey` ha
  `delete_rule = SET NULL`;
- a livello di API, con
  `test_rimuovere_un_creator_non_fa_sparire_i_suoi_insight`: dopo la DELETE del
  creator l'insight è ancora nel feed, con `creator_id` a `null`.

**Tre osservazioni minori, non corrette** (nessuna è bloccante, vedi "TODO
minori" in fondo).

### 2. CORS ristretto a `FRONTEND_URL` ✅

Confermato in `backend/app/main.py` + `Settings.cors_origins`. Fuori produzione
viene ammessa anche la variante `127.0.0.1` di `localhost`; in produzione resta
il solo `FRONTEND_URL`. Coperto da test in `backend/tests/test_hardening.py`,
compresi i casi ostili (`http://localhost:3000.evil.example`, che passerebbe un
confronto con `startswith`).

### 3. Logging strutturato con request-id ✅ (codice nuovo)

- `backend/app/core/observability.py` — `contextvars` per `request_id` e
  `user_id`, un `logging.Filter` che li copia su ogni record (anche quelli delle
  librerie di terze parti), e un `JsonLogFormatter` attivo solo in produzione.
- `backend/app/middleware/request_context.py` — middleware **ASGI puro**, non
  `BaseHTTPMiddleware`: quest'ultimo rompe la propagazione delle `contextvars`
  verso i `BackgroundTasks`, che è proprio dove il cron accoda le analisi.
- `backend/app/core/security.py` — `bind_user_id()` chiamata *dopo* la verifica
  della firma del JWT.
- `backend/app/main.py` — `configure_logging()` nel lifespan, middleware
  registrato per ultimo (= strato più esterno).

Il `request_id` **non** viene mai restituito al client (nessun header
`X-Request-ID`), come da requisito. Un `X-Request-ID` in ingresso viene accettato
solo se rispetta `^[A-Za-z0-9_-]{8,64}$` (anti log-injection).

### 4. Suite di test ✅ — **73 test, tutti verdi**

`backend/tests/`:
- `fake_supabase.py` — doppio in memoria di Supabase con builder in stile
  PostgREST. **Applica il RLS**: il client costruito con la anon key filtra le
  righe sul `sub` del JWT, quello service-role no (`BYPASSRLS`). Riproduce anche
  `ON DELETE SET NULL` e i vincoli `UNIQUE`. `FakeAPIError` estende il vero
  `postgrest.exceptions.APIError`, così passa dalla traduzione reale
  "23505 → 409".
- `conftest.py` — variabili d'ambiente impostate **prima** di importare `app`
  (in pydantic-settings l'ambiente ha precedenza sul `.env`), doppi per Gemini e
  Apify che **contano le invocazioni**, patch `autouse` del download.
- `test_analyze_flow.py` (18) — i tre requisiti richiesti: scrittura alla prima
  chiamata, cache alla seconda senza chiamate esterne, filtro `mode=SCRIPT`.
  Più: cache insensibile ai parametri di tracciamento, cache non condivisa fra
  utenti, video troppo grande → 422 senza riga su `insights`, Gemini KO → 503
  senza riga, Apify KO non blocca l'analisi, resilienza del cron.
- `test_hardening.py` — 401 su **ogni** route protetta (elencate dallo schema
  OpenAPI, così una route nuova non autenticata fa fallire la suite), JWT
  scaduto/contraffatto, motivo del 401 non distinguibile, segreto del cron,
  assenza di stack trace nelle risposte, CORS, logging correlato.
- `test_api_contract.py` — vedi punto 1.

**L'autenticazione non è mockata**: i test firmano un JWT HS256 vero e
attraversano `verify_supabase_jwt`. Con un `dependency_overrides` su
`get_current_user` i test sui 401 sarebbero verdi per costruzione.

### 5. Lint e type check ✅

`backend/pyproject.toml` — configurazione unica di pytest, ruff e mypy.
`backend/requirements-dev.txt` — pytest, pytest-asyncio, ruff, mypy (installati
nel venv). **Ruff pulito, mypy pulito** su 31 file.

Correzioni di codice preesistente emerse dai controlli:
- `config.py` — rimosso un `# type: ignore[call-arg]` diventato inutile;
- `creators.py` — `record: dict[str, Any]` (il tipo inferito non era compatibile
  con la firma di `insert`);
- `apify_service.py` — `_run_actor` ora dichiara `list[Any]` e non
  `list[dict[str, Any]]`: l'annotazione ottimistica rendeva **morti** i controlli
  `isinstance` dei chiamanti, che sono l'unica difesa contro un actor che cambia
  formato;
- `media_service.py` — `# noqa: ASYNC230` motivato sulla scrittura bloccante;
- alcune righe troppo lunghe e import riordinati (auto-fix di ruff).

### 6. Docker per lo sviluppo locale ✅

- `docker-compose.yml` (radice) — solo il backend, puntato al Supabase remoto.
  Porta host **8001** (combacia con `NEXT_PUBLIC_BACKEND_URL` del frontend).
  Monta `app/`, `prompts/` e `scripts/` in sola lettura per l'hot reload.
- `backend/scripts/check_env.py` — verifica delle variabili **prima** di
  uvicorn. Non stampa mai un valore, solo nomi ed esiti. Intercetta le insidie
  già viste su questo progetto, in particolare `SUPABASE_JWT_SECRET` valorizzata
  a stringa vuota invece che assente.

⚠️ **`docker compose up` non è mai stato eseguito**: Docker non è stato
verificato in questa sessione. Il file è scritto ma non provato.

### 7. File di deploy ✅

- `backend/render.yaml` — Blueprint completo: build, start con `$PORT`,
  `healthCheckPath: /health`, env vars con `sync: false` per i segreti,
  `CRON_SECRET` con `generateValue: true`. `SUPABASE_JWT_SECRET` è
  **volutamente assente** (dichiararla vuota causerebbe 401 ovunque). In fondo,
  commentata, la variante `runtime: docker` che abilita `ffprobe`.
- `frontend/vercel.json` — solo ciò che serve davvero: `regions: ["fra1"]`,
  Content-Type e cache del manifest. Gli header di sicurezza restano in
  `next.config.ts`.
- `frontend/VERCEL.md` — cosa va configurato in dashboard e non è esprimibile in
  `vercel.json` (Root Directory `frontend`, le tre `NEXT_PUBLIC_*`, i Redirect
  URL di Supabase).

### 8. Documentazione del cron ✅

`backend/app/cron_config.md` — contratto dell'endpoint, tre opzioni con i
rispettivi compromessi:
- **GitHub Actions** (consigliata) — workflow pronto da copiare;
- **Render Cron Job** — nessun segreto duplicato, ma a pagamento;
- **Vercel Cron** — richiede una route di appoggio nel frontend, perché Vercel
  sa invocare solo path della propria applicazione. Codice della route incluso.

Il workflow del cron **non è stato committato di proposito**: una volta nel
repository parte a ogni schedule e fallirebbe ogni 6 ore finché i secret non
esistono.

### 9. CI ✅

`.github/workflows/backend-tests.yml` — tre job: `secret-scan`,
`backend-tests` (ruff → mypy → pytest), `frontend-build` (eslint + `next build`,
che fa anche il type check di TypeScript).

`.github/scripts/scan-secrets.sh` — scansione dei file tracciati da git:
file `.env` committati, chiave `service_role` di Supabase, token dei provider,
segreti server-only nel codice del frontend, allowlist `NEXT_PUBLIC_*`.

Sulla chiave `service_role`: è riconosciuta dai tre allineamenti base64 con cui
la sottostringa `service_role` può comparire nel payload di un JWT
(`ZXJ2aWNlX3Jv`, `c2VydmljZV9y`, `cnZpY2Vfcm9s`). <!-- scan-secrets:ok --> Verificato su 300 payload
generati casualmente: 0 mancati, 0 falsi positivi sulle chiavi `anon`.

**Lo scan gira su ogni push, senza filtro di path**, a differenza dei test: il
rischio maggiore è una chiave che finisce nel *frontend*, e un filtro su
`backend/**` renderebbe il controllo cieco proprio lì. Verificato in entrambe le
direzioni: passa sull'albero attuale e intercetta quattro segreti piantati ad
arte.

### 10. Hook di enforcement ✅ (logica verificata, invocazione no)

`.claude/settings.json` + `.claude/hooks/`:

| Hook | Evento | Cosa fa |
|---|---|---|
| `block_frontend_secrets.py` | PreToolUse (Write/Edit) | Blocca scritture in `frontend/` con `GEMINI_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY` o il *valore* di una credenziale nota |
| `lint_python.py` | PostToolUse `.py` | `ruff check` sul file + `mypy` sul progetto |
| `lint_frontend.py` | PostToolUse `.ts`/`.tsx` | `eslint --fix` |
| `require_tests.py` | Stop | Non chiude il turno se ci sono modifiche in `backend/` e `pytest` fallisce |

Tutti e quattro **testati per invocazione diretta** (JSON su stdin), casi
positivi e negativi, compreso l'anti-loop dello Stop hook via `stop_hook_active`.

⚠️ **Non è stato possibile verificare che Claude Code li invochi davvero**: i
comandi usano `$CLAUDE_PROJECT_DIR`, la cui espansione su Windows dipende dalla
shell con cui Claude Code esegue gli hook. Procedura di verifica in
`.claude/hooks/README.md`, sezione "Verificare che gli hook siano attivi".

### 11. README ✅

`README.md` riscritto nella parte alta (era fermo a "solo il layer dati,
backend e frontend sono placeholder"): architettura, struttura reale del
monorepo, setup locale passo per passo, tabella di troubleshooting, qualità e
test, deploy in 4 passi, PWA e Web Share Target. La sezione **Database Schema**,
che era già ottima, è rimasta intatta.

---

## Task in corso / incomplete

### A. Review pre-deploy ❌ **NON COMPLETATA — è il primo lavoro da riprendere**

Erano stati lanciati due subagent in parallelo; **entrambi sono morti per
raggiungimento del limite di sessione** senza produrre alcun risultato
utilizzabile. Nessuna delle due review è stata fatta.

1. **`security-reviewer` su tutta la repo** (`.claude/agents/security-reviewer.md`
   esiste). Serve soprattutto sui file **nuovi, mai revisionati**:
   `backend/app/core/observability.py`, `backend/app/middleware/request_context.py`,
   `backend/scripts/check_env.py`, `backend/tests/`, `.claude/hooks/*.py`,
   `.github/scripts/scan-secrets.sh`, `docker-compose.yml`, `render.yaml`,
   `vercel.json`, il workflow CI e i tre file `.md` nuovi.
   Va detto all'agente di **non riportare mai il contenuto** di `backend/.env` e
   `frontend/.env.local`, che contengono credenziali vere.

2. **Subagent avversariale a contesto pulito** sull'endpoint `analyze-video`.
   Attenzione: **`docs/SPEC.md` non esiste** in questa repo e non c'è un PRD
   versionato — vanno passati i requisiti espliciti nel prompt (cache,
   isolamento fra tenant, modalità di analisi, edge case: video troppo
   grande/lungo, Apify in rate limit, Gemini fuori schema, SSRF, nessuna riga
   parziale su fallimento a metà pipeline, pulizia di file temporaneo e file
   remoto Gemini).

3. **`db-migration-reviewer`** — richiesto dal prompt originale *"se creato
   dall'Agente 1"*. **Non esiste**: in `.claude/agents/` ci sono solo
   `prompt-tuner.md` e `security-reviewer.md`. Step da saltare, oppure va creato
   l'agente prima.

### B. Installazione dei plugin ❌ **da fare a mano, richiede l'utente**

Il prompt iniziale chiedeva quattro plugin. **Non sono installabili da me**:
`/plugin install` è un comando dell'interfaccia di Claude Code, non uno
strumento disponibile a un agente. Vanno digitati dall'utente:

```
/plugin install github@claude-plugins-official
/plugin install vercel@claude-plugins-official
/plugin install commit-commands@claude-plugins-official
/plugin install pr-review-toolkit@claude-plugins-official
```

Nota: `security-guidance@claude-plugins-official` risulta già abilitato in
`.claude/settings.json`. Il prompt chiedeva di "farlo girare in CI su ogni
push": non è possibile alla lettera — è un plugin locale di Claude Code, non
un'azione GitHub. L'equivalente funzionante è il job `secret-scan` descritto
sopra, che fa il lavoro che quel requisito voleva.

Analogamente, `/code-review` (plugin `pr-review-toolkit`) **non è agganciabile
come check obbligatorio di GitHub**: è un comando locale. Per renderlo di fatto
obbligatorio va usata la branch protection su `main` con i check della CI, più
la convenzione di lanciare `/code-review` prima di aprire la PR.

### C. Backend locale fermo ⚠️

Il processo uvicorn su `:8001` **è stato terminato e non riavviato** (la
sessione si è interrotta a metà del riavvio). Il codice del backend è cambiato
in questa sessione — nuovo middleware — quindi va comunque riavviato:

```bash
cd backend
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8001 --reload
```

Smoke test end-to-end **mai eseguito** contro il processo reale dopo le
modifiche. Da fare: `/health` → 200, `/api/v1/insights` senza JWT → 401, CORS
con `Origin: http://localhost:3000` → header presente, e un'analisi vera dal
browser.

### D. Niente è stato committato ⚠️

Tutto è nel working tree, **su `main`**, e `main` è allineato a `origin/main`
(nessun commit locale in attesa). Prima di committare vale la pena decidere se
farlo su un branch dedicato.

Da notare: **`supabase/` risulta non tracciato** (`??` in `git status`). La
migration `0001_init.sql` non è mai stata committata pur essendo già applicata
al progetto Supabase remoto. Da includere nel commit.

Anche `backend/backend.log` e `frontend/frontend.log` risultano non tracciati:
**non vanno committati**, andrebbero aggiunti a `.gitignore`.

---

## Decisioni tecniche prese finora

### Architettura

- **Due client Supabase, non intercambiabili.** Richieste utente → anon key +
  JWT, RLS attivo (un filtro dimenticato produce zero righe, non una fuga di
  dati). Service role (`BYPASSRLS`) solo dove non esiste un JWT — scrittura
  post-inferenza e job cron — e sempre attraverso
  `service_table(tabella, user_id)`, che impone da sé il filtro sul proprietario.
  Unica eccezione: `unscoped_service_table`, vincolata da un'allowlist che oggi
  contiene solo `creators`.
- **L'identità viene sempre dal JWT verificato**, mai dal body. Gli schemi di
  richiesta hanno `extra="forbid"`.
- **`UNIQUE (user_id, video_url)` è la cache.** Perché regga, l'URL va
  normalizzato (`media_service.normalize_video_url`) prima di diventare chiave.
- **Errori**: `SafeRoute` (un `APIRoute` con try/except globale) +
  `register_exception_handlers`. Forma unica
  `{"error": {"code", "message", "details"?}}`, mai stack trace verso il client.
  Input rifiutato → 422, servizio esterno KO → 503, auth → 401.

### Convenzioni

- **Codice e commenti in italiano**, come tutto il resto del progetto. I commenti
  spiegano il *perché*, non il *cosa*.
- I messaggi d'errore di dominio sono in italiano perché li legge l'utente
  finale.
- I nomi dei test sono frasi in italiano che descrivono il comportamento
  (`test_seconda_chiamata_stesso_video_usa_la_cache`).
- Configurazione degli strumenti concentrata in `backend/pyproject.toml`, così
  hook locali e CI leggono le stesse regole.

### Scelte non ovvie, da non contraddire

- **Middleware ASGI puro** per il request context: `BaseHTTPMiddleware` romperebbe
  le `contextvars` nei `BackgroundTasks` del cron.
- **Il `request_id` non torna al client.** Requisito esplicito. Il costo è che un
  utente non può citare un id in una segnalazione: si parte da `user_id` +
  timestamp.
- **Fake al posto di mock** per Supabase: si verifica il risultato (quali righe
  tornano, cosa finisce nello store), non che sia stata chiamata una certa
  funzione. E il fake **applica il RLS**, così una query scoped che perdesse il
  filtro sul tenant fa fallire i test.
- **L'auth nei test non è mockata** (JWT HS256 vero): con un override i test sui
  401 sarebbero verdi per costruzione.
- **Le route protette si enumerano dallo schema OpenAPI**, non da `app.routes`:
  questa versione di FastAPI non appiattisce i router inclusi in `app.routes`, e
  iterarlo restituirebbe una lista vuota — un test verde che non verifica nulla.
- **Lo scan dei segreti non filtra per path**; i test sì.
- **Il limite di durata dei video dipende da `ffprobe`**: senza (runtime Python
  di Render, o Windows senza ffmpeg) resta attivo solo il limite di dimensione,
  che è comunque applicato *durante* lo streaming.
- **`GEMINI_MODEL=gemini-flash-latest`**: `gemini-2.5-flash` (il default nel
  codice) è stato ritirato per questa chiave API — Google risponde 404 anche se
  il modello compare ancora in `models.list()`.

---

## Note utili

### Comandi

```bash
# Test, lint, type check
cd backend
.venv/Scripts/python.exe -m pytest              # 73 test, ~5s, nessuna rete
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy

# Scansione segreti (dalla radice)
bash .github/scripts/scan-secrets.sh

# Verifica della configurazione
cd backend && .venv/Scripts/python.exe scripts/check_env.py

# Avvio
cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8001 --reload
cd frontend && npm run dev        # :3000
```

### Ambiente

- Backend in ascolto su **8001**, non 8000: la porta 8000 aveva dato problemi con
  processi fantasma non terminabili. `frontend/.env.local` punta già a 8001.
- Progetto Supabase: `jaimkiagtolxbkftjapx`. Migration già applicata; le tre
  tabelle esistono con RLS attivo.
- `backend/.env` e `frontend/.env.local` esistono e sono gitignored. **Non
  vanno committati e il loro contenuto non va incollato in chat.**
- Il venv è in `backend/.venv`. Dipendenze di sviluppo già installate.

### Riavvio pulito di uvicorn su Windows

`taskkill` e `pkill` in questo ambiente a volte riportano successo senza
liberare la porta (uvicorn `--reload` crea processo padre + figlio). Procedura
affidabile, in PowerShell:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*uvicorn*' } |
  Select-Object ProcessId, CommandLine
Stop-Process -Id <id1>,<id2> -Force
```

### Problemi noti

- **`ASYNC230` in `media_service.py`** — scrittura bloccante in funzione async,
  soppressa con `noqa` motivato: ogni `write` è di 256 KB verso la page cache
  (decine di microsecondi) mentre l'attesa vera è sulla rete. Spostarla in un
  threadpool aggiungerebbe un hop per chunk senza guadagno.
- **Instagram e YouTube Shorts non verificati** in
  `apify_service._single_video_input()`. Su TikTok era emerso che senza
  `shouldDownloadVideos: true` l'actor non restituisce alcun URL scaricabile
  (bug già corretto). Le altre due piattaforme **non sono state controllate per
  lo stesso problema**: se compare un 503 su un link Instagram o YouTube, è il
  primo posto da guardare.
- Due `DeprecationWarning` nei test, da dipendenze esterne (`starlette.testclient`
  con httpx, `apify-shared`). Innocui.

### ✅ RISOLTO — spesa duplicata e perdita di `creator_id` su `analyze-video`

Risolto il 9 agosto 2026, branch `fix-concurrent-upsert-race`. Erano **due
difetti distinti** con la stessa origine — nessuna coordinazione fra richieste
che scrivono sulla stessa riga di `insights` — e hanno richiesto due garanzie
separate.

**Correzione della diagnosi originale.** La nota precedente diceva che era il
cron a cancellare `creator_id` per mancanza di contesto. È il contrario, ed è
stato verificato riga per riga: il cron **passa** `creator_id`
(`cron.py:126`, da `_AnalysisJob`), mentre è il **path manuale** a ometterlo —
`analyze-video` chiama `perform_analysis` senza quel parametro, che vale `None`
per default. `_build_insight_payload` metteva comunque la chiave nel payload, e
l'upsert la riscriveva a `NULL`.

**E non era una race.** Lo scenario quotidiano non richiede concorrenza: il
cron analizza un video in `INFO`, l'utente più tardi lo richiede in `BOTH`, la
riga non copre la modalità, si rianalizza e la riscrittura cancella il creator.
Da notare che il fix `required_mode` sulla cache, introdotto l'8 agosto, ha
**aumentato** l'esposizione a questo difetto: prima qualunque riga esistente
era un cache hit e l'upsert non rigirava quasi mai.

**Garanzia 1 — spesa.** Lock a scadenza su `(user_id, video_url, analysis_mode)`
nella nuova tabella `analysis_locks` (`supabase/migrations/0003_analysis_locks.sql`,
modulo `app/services/analysis_lock.py`). Chi non ottiene il lock non rianalizza:
attende il risultato e lo restituisce come cache hit, oppure risponde `409`
(`AnalysisInProgressError`) se l'attesa scade.

Non è stato usato `pg_advisory_lock`, che sarebbe stato il meccanismo naturale:
questo backend non ha una connessione Postgres da tenere aperta — parla col
database solo via PostgREST in HTTP — e un lock di sessione preso su una
connessione poolata resterebbe orfano. Il ragionamento completo è nel commento
in testa alla migration.

**Lock orfani.** Il rilascio in `finally` è solo un'ottimizzazione: un processo
ucciso da OOM o un container riavviato non eseguono nulla in uscita. Ogni riga
porta `expires_at`, e l'acquisizione sottrae i lock scaduti con un `UPDATE`
condizionale atomico. Nessun processo di pulizia, nessun cron: la prima
richiesta che arriva dopo la scadenza riassorbe la chiave.

**I due timeout sono numeri diversi e vanno tenuti separati.**
`analysis_lock_ttl_seconds` (1200s) è un vincolo di correttezza: deve superare
il caso peggiore di un'analisi legittima — Apify 180 + download 120 + attesa
file Gemini 120 + inferenza 300 per ciascuno dei 2 tentativi = 1020s — perché
se scadesse prima, un'altra richiesta sottrarrebbe il lock a un'analisi in
corso e la doppia spesa tornerebbe. `analysis_lock_wait_seconds` (90s) è invece
una scelta di esperienza utente, e può essere molto più breve.

**Garanzia 2 — integrità.** `_build_insight_payload` **omette** `creator_id`
quando è `None`. L'upsert genera `ON CONFLICT DO UPDATE SET` sulle sole colonne
presenti, quindi una chiave assente lascia intatto il valore in archivio. Non
dipende dal lock: chiude anche il caso sequenziale.

**Prova, prima e dopo** (`tests/test_concorrenza_analisi.py`, 10 test):

| Scenario | Prima | Dopo |
|---|---|---|
| 3 POST concorrenti identici | 3 Gemini, 3 Apify, 3 download, `[201,201,201]` | **1** Gemini, **1** Apify, **1** download, `[201,200,200]` |
| Manuale + cron in parallelo | `creator_id` → `None` | `creator_id` **preservato**, 1 sola analisi |
| Cron `INFO` poi manuale `BOTH`, sequenziale | `creator_id` → `None` | `creator_id` **preservato** |

Coperti anche: sottrazione del lock scaduto, lock valido non sottraibile,
rilascio su successo e su fallimento, `409` allo scadere dell'attesa, e utenti
diversi sullo stesso video che non si bloccano a vicenda.

**Un cache hit non basta a preservare l'attribuzione.** Omettere `creator_id`
dall'upsert impedisce di *cancellarlo*, ma se due richieste corrono sullo stesso
video e vince quella manuale — che un creator non ce l'ha — l'analisi viene
scritta senza, e il cron riceve quel risultato come cache hit senza mai
attribuirlo. Nessuno sovrascrive nulla, eppure l'attribuzione manca; e non si
recupera da sé, perché `_filter_already_analyzed` fa saltare al cron i video che
hanno già un insight. Da qui `_assicura_attribuzione`, che su un cache hit
aggancia il creator a una riga che ne è priva, filtrando su PK, proprietario e
`creator_id is null`.

Il difetto è emerso **solo** eseguendo gli scenari contro il database vero: nel
doppio di test l'ordine di scheduling era quello favorevole, e il test passava
per fortuna. Ora l'ordine è forzato nel verso sfavorevole ed è stato verificato
che il test fallisca se la correzione viene disattivata.

✅ **Migration `0003` applicata al progetto remoto** il 9 agosto 2026, con
schema verificato: PK `(user_id, video_url, analysis_mode)`, RLS attivo e zero
policy, nessun privilegio ad `anon` e `authenticated`, indice su `expires_at`.
Gli scenari A/B/C sono stati poi rieseguiti contro la tabella vera con esito
verde, e le righe di prova rimosse.

### ⚠️ PRIORITÀ MEDIA — due esecuzioni del cron sovrapposte duplicano lo scraping

Emerso dalla scansione dei pattern gemelli del 9 agosto 2026. **Solo segnalato,
non corretto.**

`POST /api/v1/cron/check-updates` non ha alcuna guardia contro esecuzioni
sovrapposte: due scheduler, o un retry che parte mentre il giro precedente è
ancora in corso, enumerano entrambi i creator e chiamano entrambi
`apify.fetch_latest_videos` (`cron.py:205`) — una chiamata esterna a pagamento,
per ogni creator, senza deduplica in volo.

La parte costosa a valle **è ora protetta**: le analisi accodate passano da
`perform_analysis`, che prende il lock e ricontrolla la cache, quindi Gemini e
il download non vengono pagati due volte. Resta scoperto lo scraping.

Nota collegata: `_filter_already_analyzed` (`cron.py:84`) è un check-then-act —
legge quali URL hanno già un insight e poi accoda — ma la finestra che apriva è
adesso chiusa dal lock, per la stessa ragione.

Un secondo effetto, minore: ogni esecuzione ha il proprio
`asyncio.Semaphore(cron_max_concurrent_analyses)`, quindi due giri sovrapposti
possono raddoppiare il parallelismo rispetto al limite configurato.

Pattern controllati e **risultati sani**: `creators.py:68` è un `insert` con
vincolo `UNIQUE` tradotto in `409`, non un last-writer-wins; `update_creator`
(`creators.py:112`) scrive solo i campi effettivamente inviati
(`changed_fields()` con `exclude_none`), quindi nessun campo retrocede per
omissione; `profiles` non viene mai scritta dal backend.

### TODO minori

Emersi dall'audit dei contratti, **nessuno bloccante**, tutti nel frontend:

1. `frontend/components/search-bar.tsx` — l'input non ha `maxLength`; il backend
   accetta `search` fino a 200 caratteri e oltre risponde 422. Incollare un testo
   lungo mostra un errore generico invece di essere troncato lato client.
2. `frontend/components/creators-view.tsx` — la mutation di cancellazione
   invalida solo `["creators"]`, non `["insights"]`. Gli insight in cache
   conservano il vecchio `creator_id` fino al refetch. Effetto visivo nullo (il
   badge del creator sparisce comunque, perché la lookup fallisce), ma la cache
   resta incoerente.
3. `insights` non denormalizza l'handle del creator: cancellato il creator,
   l'attribuzione storica è persa per sempre. È coerente con `SET NULL`, ma se
   si vuole conservarla serve una colonna `creator_username` popolata alla
   scrittura.

Altro:
4. ~~Aggiungere `*.log` a `.gitignore`~~ — **fatto** l'8 agosto 2026, insieme
   alla cancellazione di `backend/backend.log`.
5. `backend/.env.example` riporta ancora `GEMINI_MODEL=gemini-2.5-flash`:
   conviene allinearlo a `gemini-flash-latest`. (Il `.env.example` di radice non
   contiene affatto quella riga: la nota precedente indicava il file sbagliato.)

8. **Normalizzazione degli URL incompleta** (review avversariale, verificato:
   9 URL dello stesso video → 9 righe → 9 inferenze). Il caso peggiore è la
   porta: `media_service.py:114` usa `parts.netloc` invece di `parts.hostname`,
   quindi `tiktok.com:443` diventa una chiave diversa da `tiktok.com` — ed è
   invisibile all'utente. Restano fuori anche `youtu.be/<id>` vs
   `/shorts/<id>`, `instagram.com/{p,reel,reels}/<id>`, il punto finale
   dell'FQDN, il percent-encoding e i redirect brevi (`vm.tiktok.com`, `/t/`),
   questi ultimi già documentati nel codice.
9. **Copertura dei test su `media_service`.** La fixture `downloads` di
   `conftest.py` è `autouse` e sostituisce `download_to_temp` in tutta la
   suite: nessun test esercitava il modulo vero. L'8 agosto 2026 sono stati
   aggiunti test diretti su `_per_hop_headers`, sul logging del download e su
   `detect_platform`, ma **restano scoperti** `normalize_video_url` (la chiave
   di cache) e `_assert_public_target` (la difesa SSRF).
   Nota collegata: `test_apify_non_raggiungibile_non_blocca_l_analisi`
   (`test_analyze_flow.py:360`) è verde per costruzione — con il codice reale
   l'URL originale di un Reel è HTML e `_guess_mime_type` solleverebbe 503. La
   proprietà "Apify non è fatale" vale solo per link diretti a `.mp4`.

### `backend/scripts/check_env.py` — due difetti aperti

Emersi l'8 agosto 2026 usando lo script per validare la configurazione dopo la
rotazione delle chiavi. Nessuno dei due è bloccante, **entrambi sono fuori dal
batch di fix pre-commit** per decisione esplicita.

6. **Non carica mai `.env`.** Lo script legge solo `os.environ` (riga 83 e
   seguenti). Dentro Docker funziona, perché `env_file` popola l'ambiente prima
   dell'entrypoint; ma il README lo documenta a riga 155 come passo del setup
   **locale non-Docker**, e lì riporta *tutte* le variabili obbligatorie come
   assenti. Correzione: una `load_dotenv()` all'avvio, oppure correggere il
   README perché lo script vada invocato solo con l'ambiente già popolato.
7. **Crash su console Windows.** `Report.errore()` e `Report.ok()` stampano `✗` e
   `✓`; con la console a cp1252 la `print` solleva `UnicodeEncodeError`. Lo
   script muore quindi **proprio quando ha un errore da segnalare** — il caso
   peggiore possibile per uno strumento diagnostico. `block_frontend_secrets.py`
   risolve lo stesso problema con `sys.stderr.reconfigure(encoding="utf-8")`:
   qui serve l'equivalente su `sys.stdout`.
