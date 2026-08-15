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

### ✅ RISOLTO — abuso di costo via insert diretti su `creators` (vettore B)

Chiuso il 9 agosto 2026, migration `0004`. Emerso dall'audit di readiness
sicurezza, sezione 2, e corretto **prima** di completare l'audit perché
sfruttabile in produzione nel momento in cui è stato misurato.

**Il vettore, misurato.** Un utente autenticato possiede publishable key e JWT,
quindi può scrivere su `creators` direttamente via PostgREST senza toccare
alcun endpoint. Misurato: **186 insert in 15 secondi (744/minuto)**, nessun
rifiuto, e tutte le righe rientravano nella SELECT del cron. A 10 risultati
Apify per creator e $2,70/1.000 risultati, un'ora di insert costava **$1.205
per singola esecuzione del cron**, ripetuta a ogni giro.

**Perché un tetto e non una revoca.** La revoca — la strada della `0002` per
`profiles` — qui avrebbe rotto l'applicazione: `POST`, `PATCH` e `DELETE
/api/v1/creators` scrivono con la chiave dell'utente (`scoped_client`), non con
la service role. Verificato riga per riga prima di decidere. Spostarli al
bypass del RLS avrebbe contraddetto la scelta architetturale dichiarata nel
README.

**La correzione.** Trigger `creators_enforce_limit` che conta i creator
**attivi** per utente e li limita in base a `profiles.subscription_tier`:
`free` → 30, `pro` → 200 (segnaposto, da fissare contro il prezzo del piano).
Il conteggio guarda solo `is_active` — chi cancella o disattiva libera uno slot
— e il controllo scatta anche sull'UPDATE, ma solo sulla transizione che
aumenta gli attivi: senza, bastava creare al tetto, disattivare, creare altri e
riattivare tutti.

Il numero viene dal costo misurato: ~$0,140 al giorno per creator attivo
(Apify $0,108 + Gemini ~$0,032), quindi **~$4,20 al giorno** per un utente al
tetto, sotto la soglia di $5 fissata.

In più: **revocati INSERT/UPDATE/DELETE su `insights`** ad `authenticated`.
Verificato che con la chiave utente il backend lì faccia solo SELECT. Chiude la
possibilità di fabbricare righe di `insights`, che sarebbe stata una via per
falsificare qualunque contatore di quota basato su di esse.

**Prova** (`tests/test_limite_piano.py` per la traduzione dell'errore, più
verifica sul progetto reale con utente usa e getta):

| Prova | Esito |
|---|---|
| Insert diretti PostgREST da zero | si fermano a **30 esatti**, poi HTTP 400 `PX001` |
| 31° creator dal backend | **409 `plan_limit_reached`**, messaggio pulito |
| Dettagli interni nella risposta | **nessuno** (né SQLSTATE, né nome funzione, né tabella) |
| DELETE poi CREATE | 201 — lo slot si libera |
| PATCH `is_active=false` poi CREATE | 201 — contano solo gli attivi |
| Riattivazione mentre si è al tetto | **409** — la scappatoia è chiusa |
| `GET /creators`, `GET /insights` | 200 — lettura intatta dopo la revoca |

### ✅ VERIFICATO — nessun segreto è mai stato committato (audit sezione 4)

Eseguito il **10 agosto 2026**. Risponde alla domanda che `scan-secrets.sh` non
può coprire: quello guarda l'albero corrente, quindi un segreto committato e poi
rimosso gli resterebbe invisibile.

**Metodo.** Non uno scanner generico, ma i **pattern già calibrati** di
`.github/scripts/scan-secrets.sh` — tarati su questo codice per non dare falsi
positivi su token di test e regex del detector — applicati allo storico per tre
vie complementari:

1. `git grep` su **tutti i 14 commit** raggiungibili da *ogni* ref (non solo
   `main`, non solo `HEAD`);
2. spazzata di **tutti i 180 blob dell'object database**, inclusi i **16
   irraggiungibili** — quelli di commit riscritti da rebase o amend, che
   `rev-list --all` non vede e che uno scan sui soli ref mancherebbe;
3. elenco dei **135 path distinti mai esistiti** in storico, per intercettare un
   `.env` o un file di log entrato e poi tolto.

Ai pattern calibrati sono stati aggiunti, solo per lo storico, JWT generico,
AWS `AKIA`, token GitHub e Slack, e l'assegnazione di una variabile server-only
con valore di lunghezza reale: formati che il codice di oggi non usa ma che un
commit vecchio potrebbe contenere.

**Control group.** I pattern sono stati verificati contro un file-esca con
credenziali sintetiche di ciascun formato (JWT `service_role`, `sb_secret_`,
`apify_api_`, `AIza`): tutti intercettati. Senza questa prova, "zero hit"
avrebbe potuto significare "regex cieca". Il file non è mai entrato nel repo.

**Cross-check indipendente.** `detect-secrets` 1.5.0 (27 plugin, inclusi quelli
entropici) su tutti i 180 blob materializzati su disco, per coprire formati che
i pattern calibrati non prevedono. 1.469 finding, tutti classificati e tutti
falsi positivi: **1.460** sono gli hash `integrity` `sha512-` di due versioni di
`frontend/package-lock.json`, i restanti 9 sono le costanti letterali dei test
(`test-cron-secret`, `test-jwt-secret`, `test-gemini-key`, `"sbagliato"`) e le
fixture con token dichiaratamente finti. Installato in un venv usa e getta,
rimosso a fine lavoro: **non è una dipendenza del progetto**.

**Esito: pulito.** Nessuna credenziale reale in alcun commit, in alcun branch,
in alcun blob. In particolare:

| Verifica | Esito |
|---|---|
| Chiave `service_role` / `sb_secret_` in storico | **nessuna** |
| Token Apify / Gemini / OpenAI / AWS / GitHub / Slack | **nessuno** |
| Chiavi private PEM | **nessuna** |
| File `.env` mai tracciato | **nessuno** (solo i tre `.example`) |
| `backend.log` mai entrato in un commit | **confermato** |

L'ultimo punto è quello che conta di più: la `service_role` key trapelata e
ruotata il 9 agosto 2026 stava in `backend.log`, un file **non tracciato**. La
scansione conferma che non è mai finita in un commit, quindi la rotazione già
fatta chiude il caso e **non serve alcuna riscrittura della cronologia**.

Nota minore, senza impatto: fra i blob irraggiungibili c'è una chiave
`sb_publishable_`, residuo delle verifiche degli hook. È pubblica per
progettazione — è la chiave che sta nel bundle del browser — ed è comunque in un
oggetto che nessun ref raggiunge.

### ⚠️ MINORE — un 500 fuori da `SafeRoute` non porta gli header CORS

Emerso dall'audit di readiness, sezione 5, il **10 agosto 2026**. Non è un leak:
la risposta è sanificata in entrambi i casi. È un problema di *leggibilità* per
il client.

**Perché.** Lo stack dei middleware, dall'esterno verso l'interno, è
`ServerErrorMiddleware → RequestContextMiddleware → CORSMiddleware →
ExceptionMiddleware → router`. Le due reti di sicurezza stanno quindi ad altezze
diverse:

* un'eccezione dentro un router con `route_class=SafeRoute` viene convertita in
  `PicoxError` e gestita da `ExceptionMiddleware`, che sta **dentro** al CORS →
  la risposta esce con `Access-Control-Allow-Origin`;
* un'eccezione in un endpoint **senza** `SafeRoute` arriva fino a
  `ServerErrorMiddleware`, che sta **fuori** dal CORS → stessa identica risposta
  JSON, ma **senza** header CORS.

Misurato con origin ammesso (`http://localhost:3000`):

| Endpoint | Risposta | Header CORS |
|---|---|---|
| con `SafeRoute` | `500 internal_error` | `access-control-allow-origin` presente |
| senza `SafeRoute` | `500 internal_error` (byte identici) | **assente** |

**Conseguenza pratica.** Un browser non può leggere quel 500: vede un errore
CORS e il frontend mostra "errore di rete" invece dell'envelope. Il caso non è
teorico — `/health` in `main.py` è già registrato così, direttamente sull'app.

**Non è urgente** perché non espone nulla, e perché tutti i router applicativi
usano `SafeRoute`. Va però tenuto presente quando si aggiunge un endpoint: la
convenzione `route_class=SafeRoute` non serve solo a sanificare (quello lo fa
comunque la rete esterna), serve a far arrivare l'errore al browser.

Nota minore collegata: il rifiuto di una preflight CORS da origin non ammesso
risponde `400 text/plain "Disallowed CORS origin"`, generato da Starlette e
quindi fuori dall'envelope `{"error": {...}}` usato ovunque altrove. Nessun
dettaglio interno, solo un formato incoerente.

### ✅ VERIFICATO — nessun leak di informazioni negli errori (audit sezione 5)

Eseguito il **10 agosto 2026** contro il backend vivo con `ENVIRONMENT=production`,
non leggendo il codice: **28 scenari** più i fallimenti reali dei servizi
esterni, ognuno con la risposta HTTP ispezionata byte per byte.

**Parte A — nessun leak, in nessuno scenario.** Zero occorrenze di stack trace,
path interni, SQLSTATE, nomi di tabella, frammenti SQL, URL o credenziali.
Verificato con una stringa-esca piantata dentro il messaggio delle eccezioni
(DSN con password, path sorgente, token): compare **8 volte nei log e 0 volte
nelle risposte**.

| Scenario | Risposta | Esito |
|---|---|---|
| `TypeError` / `AttributeError` non gestiti | `500 internal_error`, 86 byte | pulito |
| Eccezione con credenziali nel messaggio | `500 internal_error`, 86 byte | pulito |
| Errore di rete reale (URL interno nel messaggio) | `500 internal_error` | pulito |
| Validazione Pydantic (5 varianti) | `422 validation_error` + `field`/`message`/`type` | pulito |
| Violazione `UNIQUE` su `creators` | `409 conflict` | pulito |
| PostgREST su tabella inesistente | `503 database_unavailable` | pulito |
| Gemini con chiave non valida (chiamata reale) | `503 gemini_unavailable` | pulito |
| Apify con token non valido (chiamata reale) | `503 external_service_error` | pulito |
| 401 (assente / malformato / firma errata) | `401 unauthorized` | pulito |
| Cron senza segreto e con segreto errato | `401 unauthorized` | pulito |
| `405`, rotta inesistente, `/docs`, `/openapi.json` | `405` / `404` | pulito |

Il 422 espone `field`, `message` e `type` di Pydantic: sono i nomi dei campi
**del contratto pubblico**, non della struttura interna, e `_validation_details`
scarta di proposito `input` e `ctx`, che rimanderebbero indietro il valore
inviato. Corretto così: senza il nome del campo l'errore sarebbe inutilizzabile.

**404 su risorsa altrui vs inesistente: indistinguibili.** Stesso status, corpo
**identico byte per byte**, header identici, e nessun canale temporale — mediane
su 8 ripetizioni: `PATCH` 190,1 ms vs 191,5 ms (Δ 1,5 ms), `DELETE` 192,7 ms vs
187,9 ms (Δ 4,8 ms). Non è possibile enumerare risorse altrui per differenza di
risposta.

**Separazione log / risposta, provata caso per caso.** Nei log ci sono 38
traceback completi, i nomi delle funzioni sorgente e il nome della tabella
rifiutata dal database; nelle risposte, nessuno di questi frammenti. In più:
**zero occorrenze** dei segreti reali (`SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_ANON_KEY`, `GEMINI_API_KEY`, `APIFY_API_TOKEN`, `CRON_SECRET`) nei
125 KB di log prodotti — regressione del fix hpack/Apify del 9 agosto, ancora
tenuta. Log JSON strutturato con `request_id` per correlare le due sponde.

**Parte B — la garanzia strutturale esiste, ed è a due strati.** Verificata
attivamente, non letta: un endpoint registrato **senza** `SafeRoute` — cioè
scritto come lo scriverebbe chi non conosce la convenzione — risponde comunque
`500 internal_error` sanificato, byte per byte identico a quello protetto.
`@app.exception_handler(Exception)` (`error_handler.py:168`) finisce in
`ServerErrorMiddleware`, che è il livello più esterno dello stack: **nessun
endpoint futuro può bypassarlo**, perché non c'è nulla sopra di lui. La
protezione è per costruzione, non per disciplina. Unico limite, sopra: gli
header CORS.

**Il `debug` di Starlette non è raggiungibile.** `FastAPI(debug=...)` non viene
mai passato e nessuna variabile d'ambiente lo tocca: `app.debug` e
`ServerErrorMiddleware.debug` sono `False` per costruzione. Non è "legato a
`ENVIRONMENT`" — è più forte, l'interruttore non esiste proprio. La posta in
gioco, misurata su un'app isolata: con `debug=True` la stessa eccezione produce
**2.534 byte con traceback e password in chiaro** invece di 21.

### ✅ RISOLTO — auto-promozione a `pro`, e il downgrade che non retroagiva

Chiuso l'**11 agosto 2026**, migration `0006` e `0007`. Sono le voci **A1** e
**A2** di `SECURITY_AUDIT.md`, affrontate insieme perché la seconda dipendeva
dallo schema creato dalla prima.

**A1 — il rischio era reale, e l'ho misurato prima di correggerlo.** La `0001`
definiva su `profiles` una policy di UPDATE che copre l'intera riga; a reggere
era il solo GRANT revocato dalla `0002`. Riprodotto sul progetto, in transazione
poi annullata: come ruolo `authenticated`, con `grant update on public.profiles
to authenticated`, l'auto-promozione a `pro` **riesce**. Senza quel gruppo di
controllo la prova successiva sarebbe potuta essere verde solo perché l'utente
non poteva scrivere comunque.

Lo stato di pagamento vive ora in `public.subscriptions` e
`profiles.subscription_tier` **è rimossa**, non deprecata: una colonna morta
ricrea il rischio, perché scrivere in una colonna inutilizzata riesce sempre.
Verificato prima di rimuoverla che nessun codice la leggesse *né* la scrivesse.

Rieseguito l'attacco sullo schema applicato:

| Tentativo | Esito |
|---|---|
| `update profiles set subscription_tier` con GRANT largo | **`42703`** — la colonna non esiste |
| `update subscriptions set tier` con `GRANT UPDATE` | **`42501`** — serve anche `SELECT` per il `WHERE` |
| `update subscriptions set tier` con **`GRANT ALL`** | **0 righe viste, 0 toccate** — RLS a zero policy |

L'ultimo è il caso che conta: anche concedendo tutto per errore resta in piedi un
secondo strato indipendente.

**A2 — la disattivazione automatica.** Il criterio è
`order by created_at desc, id desc`: si mantengono i più vecchi. È spiegabile in
una frase, non dipende da `insights` (il cui segnale ha buchi: `ON DELETE SET
NULL`, e un creator aggiunto ieri avrebbe zero insight), e lo spareggio su `id`
rende l'ordine totale anche con `created_at` identici da inserimento in blocco.
La disattivazione **non è distruttiva**: riga e insight restano.

Prova end-to-end su utente usa e getta, poi eliminato:

| Scenario | Esito |
|---|---|
| `pro` con 35 creator attivi → `free` | **30 attivi, 5 disattivati** |
| Quali restano / quali escono | `creator_01…30` / `creator_31…35` |
| Righe conservate | **35 su 35** |
| Upgrade `free` → `pro` | nulla disattivato, nulla riattivato da sé |
| Riattivazione da `pro`, poi nuovo downgrade | il riattivato è di nuovo il primo a uscire |

**In più**: `creator_limit_for_tier(text)` porta i numeri del tetto in un posto
solo, usata da entrambi i trigger. Senza, lo stesso `CASE` sarebbe esistito due
volte — la divergenza segnalata come B3 nell'audit. Verificato in esercizio:
`free → 30`, `pro → 200`, `null → 30`.

> Le migration `0006` e `0007` **sono applicate** al progetto. Verificato dopo:
> 2 righe migrate, colonna vecchia assente, entrambi i trigger abilitati,
> `authenticated` senza alcun privilegio su `subscriptions`, RLS attivo con zero
> policy. Utente di prova eliminato con verifica indipendente: **0 residui**.

### ✅ RISOLTO — il vettore A: nessun tetto sulle analisi manuali

Chiuso l'**11 agosto 2026**, migration `0008`. È la voce **A3** di
`SECURITY_AUDIT.md`, l'ultima rossa rimasta.

`POST /api/v1/analyze-video` non aveva né rate limiter né quota: misurate **64
richieste/minuto accettate in sequenza, zero 429, 10 richieste concorrenti su 10
accettate** — cioè **$37–$175 all'ora per singolo account**. Il tetto ai creator
della `0004` non lo copriva: quello limita il cron, questo è il percorso manuale.

**Il meccanismo.** Tabella append-only `analysis_events`, una riga per analisi
avviata, più il trigger `enforce_analysis_quota` che rifiuta oltre il tetto del
giorno con SQLSTATE `PX002` → `AnalysisQuotaError` (409
`analysis_quota_reached`), distinto da `plan_limit_reached`.

**Il punto in cui si consuma** è dentro il lock, dopo entrambi i controlli di
cache e prima di `_esegui_analisi`. Le alternative sbagliano in direzioni
opposte: più in alto si conterebbero i cache hit, che non costano nulla; più in
basso non si conterebbero le analisi che pagano Apify e poi falliscono su Gemini
— **le stesse che non lasciano riga in `insights`**, ed è il motivo per cui
contare gli insight avrebbe sottostimato proprio l'abuso.

Append-only e non un contatore aggregato: l'incremento richiederebbe una RPC — un
upsert PostgREST non può esprimere `conteggio = conteggio + 1` — e le righe
grezze sono anche il dato che serve a misurare i costi reali (voce B4), che prima
non esisteva da nessuna parte.

**Quota solo sul percorso manuale**: il cron passa `conta_quota=False`, perché ha
già il proprio budget dal tetto ai creator. Due budget indipendenti, altrimenti
un giro notturno azzererebbe le analisi possibili di giorno.

`analysis_limit_for_tier` affianca `creator_limit_for_tier`: `free` 30/giorno
(~$1,05), `pro` 300/giorno (~$10,50), su ~$0,035 per analisi. **Segnaposto
dichiarati**, derivati e non misurati.

**Prova: 18 controlli, backend reale contro database reale**, con Gemini e Apify
sostituiti da doppi (trigger vero, spesa zero):

| Prova | Esito |
|---|---|
| 30ª analisi / 31ª | `201` / **`409 analysis_quota_reached`** |
| Dettagli interni nel corpo del 409 | **nessuno** |
| Il rifiuto consuma quota? | **no** |
| Cache hit a quota esaurita | `200`, contatore invariato |
| Gemini KO | `503`, nessun insight, **ma quota consumata** |
| Cron a quota esaurita | procede, e non consuma la quota manuale |
| **5 richieste concorrenti, 1 posto** | **1×`201`, 4×`409`** |
| Piano `pro` oltre 30 | `201` |

Più 11 test nella suite (`tests/test_quota_analisi.py`), che coprono ciò che il
doppio in memoria può riprodurre: chi consuma e chi no, e la traduzione
dell'errore.

> ⚠️ **Un errore commesso durante la verifica.** Lo scenario sul cron è stato
> eseguito invocando il cron **vero** contro il database reale. Il cron enumera i
> creator attivi di *tutti* gli utenti — è il suo scopo — quindi ha scritto due
> insight fasulli nel feed di un utente reale. Rimossi per id espliciti, con
> verifica che restasse solo la riga legittima; nessun costo, nessuna perdita di
> dati, nessuna quota consumata all'utente. **La regola che ne esce: contro il
> database reale non si invoca mai il cron**, perché è l'unico endpoint il cui
> perimetro non è l'utente della richiesta.

> `analysis_events` cresce senza limite e va potata oltre una finestra di
> ritenzione. Non è stato scritto un job: sarebbe il secondo pianificato, e oggi
> non c'è nemmeno il primo. Il meccanismo però esiste già — la `0005` ha reso
> `job_locks` generica sul nome del job esattamente per questo.

### ✅ RISOLTO — batch di chiusura delle voci minori (A6, A8, A10-SSRF, A11, A12)

Chiuse l'**11 agosto 2026**. Nessuna richiedeva decisioni, quindi affrontate in
un giro solo. Lo stato di ciascuna è stato **riverificato sul codice** prima di
agire, non dato per buono dalle note.

**A6 — il 500 fuori da `SafeRoute` non portava gli header CORS.** Lo stack è
`ServerErrorMiddleware → RequestContextMiddleware → CORSMiddleware →
ExceptionMiddleware → router`: un'eccezione in un endpoint senza `SafeRoute`
arriva al livello più esterno, che sta **fuori** dal CORS. La risposta era
sanificata ma illeggibile per il browser, che mostrava "errore di rete" invece
dell'envelope — e `/health` è registrato proprio così.

`ServerErrorMiddleware` **non si è spostato**: è la sua posizione esterna a
garantire che nessun endpoint futuro lo scavalchi. Sono gli header a scendere lì,
con `_cors_headers(request)` che confronta l'`Origin` **per uguaglianza esatta**
contro `settings.cors_origins`. 5 test in `tests/test_cors_errori.py`; quello che
conta è l'origin ostile — riflettere l'`Origin` con `Allow-Credentials: true`
avrebbe trasformato la risposta d'errore nel buco che il CORS ristretto chiude.

**A7 — il preflight fuori envelope: deciso di non correggerlo.** Rivalutato
insieme ad A6 e confermato. Il corpo di una preflight non è mai mostrato né
all'utente né al JavaScript, quindi il beneficio è zero mentre il costo è una
sottoclasse di `CORSMiddleware` da rileggere a ogni aggiornamento di Starlette.
**Chiusa per scelta, non per omissione.**

**A8 — `netloc` invece di `hostname` nella chiave di cache.** `tiktok.com:443`
produceva una chiave diversa da `tiktok.com`: stesso video, due righe, **due
inferenze pagate**, e invisibile all'utente. `hostname` normalizza da sé
minuscole, credenziali e porta. Verificata prima l'assenza di sovrapposizione con
**A9**: FQDN con punto finale, forme di path per piattaforma, percent-encoding e
redirect brevi restano fuori. 12 test in `tests/test_normalizzazione_host.py`,
con gruppo di controllo che riproduce la divergenza del vecchio codice.

**A10 — la difesa SSRF non aveva un solo test.** 23 test in `tests/test_ssrf.py`,
nessuno dei quali tocca la rete. Coprono loopback, metadata cloud
`169.254.169.254`, link-local, i tre range privati, CGNAT, multicast e i
corrispondenti IPv6; il **DNS con più record di cui uno solo interno**; gli schemi
non-HTTP rifiutati prima di risolvere; il DNS irraggiungibile che dà 503 e non
422; e soprattutto il **redirect verso la rete interna**, dove si verifica che la
richiesta all'indirizzo interno **non venga mai emessa**. La parte su
`normalize_video_url` resta deliberatamente aperta con A9.

**A11 — `check_env.py`.** Ora carica `backend/.env` e poi il `.env` di root con
`override=False`: **l'ordine conta**, perché con `override=False` vince il primo
caricato, quindi `backend/.env` va per primo per riprodurre la precedenza di
`config.py`. `python-dotenv` è stato dichiarato in `requirements.txt` — arrivava
comunque da pydantic-settings, ma usarlo senza dichiararlo era fortuna, non
intenzione. Sul crash cp1252 il pattern di `block_frontend_secrets.py`
riconfigura `stderr`, ma qui il report esce da `print`: serviva **stdout**.
Provato: col codice vecchio `UnicodeEncodeError`, con quello nuovo exit 0.

**A12 — i TODO minori.** `maxLength={200}` su `search-bar.tsx` (lo stesso limite
di `Query(max_length=200)`); la mutation di cancellazione in `creators-view.tsx`
invalida ora anche il prefisso `["insights"]`; `.env.example` allineato a
`gemini-flash-latest`. Il quarto punto — denormalizzare l'handle del creator in
`insights` — **non è stato toccato**: non è un difetto ma una conseguenza
coerente di `ON DELETE SET NULL`, e aggiungere `creator_username` è una scelta di
prodotto.

**190 test verdi** (150 → 190), ruff, mypy, `tsc --noEmit`, eslint e build del
frontend tutti puliti, scan segreti pulito.

### ✅ RISOLTO — canonicalizzazione degli URL (A9), e la copertura di A10 completata

Chiusa l'**11 agosto 2026**. Ogni chiave diversa per lo stesso video è
un'analisi pagata due volte: la review avversariale aveva misurato 9 URL dello
stesso video → 9 righe → 9 inferenze.

**Il vincolo che decide tutto, trovato verificando prima di progettare.**
`insights.video_url` — cioè la chiave normalizzata — finisce in un `href`
cliccabile (`insight-card.tsx:68`). **La forma canonica è l'URL che l'utente
apre**, non solo una chiave. Da qui il limite a quanto si può canonicalizzare.

**Un bug attivo, corretto.** `_HOST_ALIASES` riscriveva `vm.tiktok.com` →
`tiktok.com` lasciando il path del link breve: `vm.tiktok.com/ZMabc` diventava
`https://tiktok.com/ZMabc`, **un indirizzo che non esiste**, mostrato all'utente
come link. Ora gli alias vivono dentro `_Piattaforma`, e il commento spiega
perché i domini di link brevi non possono starci.

**Struttura estendibile**: `_Piattaforma(host, alias, percorsi, canonico)` e la
tupla `_PIATTAFORME`. Aggiungere una piattaforma è **una entry**, non una
modifica a `normalize_video_url`, che infatti non nomina più alcuna piattaforma.
Implementate **solo Instagram e TikTok**: la entry YouTube non è anticipata.

| | Prima | Dopo |
|---|---|---|
| Instagram `/p/`, `/reel/`, `/reels/`, `/tv/` | 4 chiavi | **1** |
| TikTok con/senza `www.`, `m.`, slash, tracking | più chiavi | **1** |
| Punto finale dell'FQDN, percent-encoding | 2 chiavi ciascuno | **1** |
| `vm.tiktok.com/ZMabc` | **URL rotto** | resta sé stesso |

**51 test** con gruppo di controllo per ogni equivalenza: `_vecchia_normalize`
riproduce la logica precedente, e ogni caso asserisce prima che quella desse due
chiavi diverse. Coperti anche i casi che **non** devono collassare, l'idempotenza
e la navigabilità della chiave. Include la copertura di `normalize_video_url`
differita con **A10**, che risulta ora chiusa in entrambe le parti.

**Due cose non fatte, e perché.** I **link brevi** non si risolvono: una `HEAD`
metterebbe una chiamata di rete nel percorso della chiave di cache, su ogni
richiesta compresi i cache hit — e Apify risolve già il link, quindi la via
migliore è ri-chiavare dopo lo scraping, che però tocca `perform_analysis` e il
lock. La **chiave sull'ID del video** sarebbe più robusta (lo stesso video TikTok
è raggiungibile con username diversi) ma richiede di separare la chiave dal
valore mostrato: colonna nuova, `UNIQUE` spostato, migration con backfill. È una
modifica di schema, non di normalizzazione — **decisione aperta**, e oggi costa
quanto non costerà mai più: 1 riga in `insights`.

### ✅ RISOLTO — `cache_key`: l'identità del video separata dall'URL mostrato (A9, punto 3)

Chiuso l'**11 agosto 2026**, migration `0009`. `UNIQUE (user_id, video_url)`
faceva fare a una colonna due mestieri incompatibili: `video_url` finisce in un
`href` cliccabile e deve restare navigabile, ma come **identità** vorrebbe
unificare di più — lo stesso video TikTok è raggiungibile con **username diversi**
nel path, e finché la chiave era l'URL si pagavano due analisi.

**Collisioni verificate prima del DDL**, nel punto esatto (dopo il backfill,
prima del `NOT NULL`), dentro una transazione poi annullata: 1 riga totale, 0
senza chiave, **0 gruppi in collisione**, 0 righe che si perderebbero.

**Perché non `NULL` per gli URL senza id.** Un link diretto a un `.mp4` non ha
un id da estrarre, e lasciare `cache_key` a `NULL` sarebbe l'errore naturale: in
PostgreSQL **due `NULL` non collidono**, quindi un vincolo di unicità su colonna
nullable non deduplica proprio le righe che la lasciano vuota. Si ricade
sull'URL normalizzato.

**`analysis_locks` ri-chiavata, e non per coerenza estetica.** Con la cache su
`cache_key` e il lock su `video_url`, due richieste concorrenti sullo stesso
video con username diversi otterrebbero **lock distinti**, procederebbero
entrambe e pagherebbero entrambe: il difetto che la `0003` esiste per chiudere,
riaperto dalla porta di servizio. Un `rename` e non una colonna nuova, così PK,
indice e privilegi seguono da soli — verificato dopo:
`PK = (user_id, cache_key, analysis_mode)`. **Il meccanismo non cambia**:
restano i due passi atomici e lo stesso TTL, cambia la stringa che fa da chiave.

**Tutti e quattro i punti allineati**, perché basta che uno solo diverga:

| Punto | Stato |
|---|---|
| `find_cached_insight` (due rami, scoped e service-role) | su `cache_key` |
| `ON CONFLICT` dell'upsert | `user_id,cache_key` |
| `analysis_locks` | ri-chiavata |
| `_filter_already_analyzed` (dedup del cron) | su `cache_key` |

L'ultimo non era nella lista iniziale ed è emerso dalla ricognizione: senza,
il cron riaccoderebbe un video già analizzato sotto altra forma — **ripagandolo**,
sul percorso automatico invece che manuale.

La chiave si deriva **una volta sola** dentro `perform_analysis` e non è un
parametro: passarla dall'esterno aprirebbe la possibilità che due chiamanti ne
calcolino versioni diverse, cioè esattamente la divergenza da chiudere.

**Il fix dell'omissione di `creator_id` regge** sulla clausola nuova, e non è
dato per scontato: dipende da quali colonne stanno nel *payload*, non nel
target. C'è un test che analizza con creator, rianalizza in altra modalità
senza creator, e verifica che l'attribuzione sopravviva.

**9 test** in `tests/test_chiave_unificata.py`, uno per punto più i gruppi di
controllo — incluso quello di concorrenza: due richieste parallele sui due URL
→ **1 sola inferenza**, risposte `200` e `201`.

### ⚠️ PRIORITÀ BASSA — i link brevi non sono risolti (A9, punto 2)

`vm.tiktok.com/ZMabc` e l'URL completo dello stesso video restano **due chiavi
di cache distinte**: chi incolla il link breve paga un'analisi che esiste già.

**Non risolto di proposito.** Una richiesta `HEAD` dentro `normalize_video_url`
metterebbe una chiamata di rete nel percorso della chiave di cache, quindi su
*ogni* richiesta — compresi i cache hit, che oggi non costano nulla — con i suoi
timeout e i suoi fallimenti. E la funzione è oggi pura e sincrona, chiamata in
cima a `perform_analysis`: renderla `async` e fallibile ne cambia il contratto
ovunque.

**La via migliore non richiede alcuna richiesta aggiuntiva.** Apify **risolve
già** il link breve e restituisce l'URL canonico in `ScrapedVideo.video_url`:
basterebbe ri-chiavare l'insight su quello **dopo** lo scraping, invece che
sull'URL in ingresso. Tocca però `perform_analysis` e il lock — il lock viene
preso *prima* di sapere l'URL risolto — quindi è un intervento a sé, non un
dettaglio di A9.

Da valutare insieme alla canonicalizzazione su ID (A9 punto 3): le due cose
condividono la stessa domanda, cioè quale valore sia la chiave.

### ⏸️ A13 — verifica di `docker compose` **non eseguibile**

Tentata l'11 agosto 2026. **Docker non è installato**, verificato su tre vie
indipendenti: assente dal `PATH` di bash e di PowerShell, servizio
`com.docker.service` inesistente, Docker Desktop non presente nei percorsi
standard. Non è un problema del `docker-compose.yml`: manca lo strumento.

Conta più di quanto sembri: `render.yaml` usa `runtime: docker` con lo stesso
`Dockerfile`, e la ragione dichiarata è che senza `ffmpeg` la durata del video
non sarebbe verificabile e `MAX_VIDEO_DURATION_SECONDS` non verrebbe applicata.
Quella catena non è mai stata provata end-to-end.

#### Verifica **statica** del Dockerfile, 11 agosto 2026

**Non sostituisce la verifica end-to-end**: nulla è stato costruito né eseguito.
Si è controllato ciò che si può controllare leggendo.

| Controllo | Esito |
|---|---|
| Nome del pacchetto `ffmpeg` | corretto, e **fornisce `ffprobe`** — verificato sulla documentazione Debian, non a memoria |
| Ordine dei layer | corretto: `requirements.txt` copiato e installato **prima** del codice, quindi la cache delle dipendenze non si invalida a ogni modifica |
| `apt-get` | `update`, `install` e `rm -rf /var/lib/apt/lists/*` nello **stesso** `RUN`: nessun layer con la cache apt dentro l'immagine |
| Permessi | `chown -R` su `/app` **dopo** `COPY . .`, poi `USER picox`: la proprieta' e' corretta e il processo non gira da root |

**Un difetto trovato, e corretto l'11 agosto: l'immagine base non era
pinnata.** `python:3.11-slim` non fissa la suite Debian, e quel tag mappa oggi su
**trixie** (Debian 13) mentre puntava a bookworm (Debian 12): lo stesso
Dockerfile costruito a distanza di mesi produceva un sistema operativo diverso,
e con esso un `ffmpeg` diverso, da 5.1 a 7.1.

Pinnato a **`python:3.11-slim-trixie`**, e la scelta della suite merita una
riga. Pinnare è stato chiesto «alla suite in uso quando il runtime Docker fu
introdotto **e testato**»: quella suite **non esiste**. Il `Dockerfile` è del 6
agosto e non è mai stato modificato, non c'è alcun riferimento a una versione di
`ffmpeg` verificata, e questa voce nasce proprio dal fatto che
`docker compose up` non è mai stato eseguito — non c'è un «già funzionante» da
preservare. Trixie è ciò che l'alias risolve oggi, quindi il pin **non cambia
nulla** di ciò che un build produce: lo rende esplicito. Tornare a bookworm
sarebbe stato un downgrade reale travestito da stabilizzazione.

**Nota minore, non un difetto**: `chown -R` riscrive i metadati di ogni file e
duplica il layer di `/app`. `COPY --chown=picox:picox . .` lo eviterebbe, al
prezzo di creare l'utente prima della copia. Irrilevante a questa dimensione.

### ⚠️ RISCHIO RESIDUO — attacco Sybil con più account

Il tetto limita il danno di **un** account, non di molti. Chi registra N
account ottiene N × 30 creator attivi, e il costo torna a scalare linearmente.

L'unica barriera oggi è la **verifica email di Supabase Auth**, che alza il
costo dell'attacco ma non lo impedisce: gli indirizzi usa e getta sono gratuiti
e automatizzabili.

**Il segnale log-only richiesto l'11 agosto non è stato implementato**, e non
per pigrizia. Quattro fatti misurati:

1. un trigger su `auth.users` vede `inet_client_addr()` = l'indirizzo di
   **GoTrue**, non dell'utente, e `request.headers` è `null` (lo imposta
   PostgREST, non GoTrue): registrarlo darebbe una costante che *sembra* un IP;
2. il backend **non vede mai il signup** — `auth-form.tsx:76` chiama
   `supabase.auth.signUp` direttamente;
3. il dato **esiste già**: `auth.audit_log_entries.ip_address`, popolata da
   GoTrue. Zero codice. (Oggi la tabella è vuota: da capire se sia retention
   prima di contarci per un'analisi storica.)
4. il signup è già limitato, ma **non da un controllo di sicurezza**: misurato
   `HTTP 429 over_email_send_rate_limit`, cioè la quota dell'SMTP predefinito di
   Supabase. **Sparisce configurando un SMTP proprio**, cioè al lancio.

Il punto 4 corregge questa voce: la barriera non è solo la verifica email, è che
quelle email le manda un SMTP con quota bassa. Chi pianifica il lancio deve
sapere che **configurare l'SMTP rimuove una protezione senza che nulla lo
segnali**.

Nessun'altra azione ora — è una decisione di prodotto che va presa insieme al
billing. Le opzioni sul tavolo: CAPTCHA al signup, limite di registrazioni per
IP o per dominio email, verifica della carta anche sul piano gratuito, oppure
un tetto globale di spesa per progetto lato Apify/Gemini come rete di sicurezza
indipendente dal numero di account.

### ⚠️ DA FARE — il vettore A resta aperto: nessun rate limit su `analyze-video`

**Questo fix non lo chiude.** L'endpoint `POST /api/v1/analyze-video` non ha né
rate limiter né tetto di concorrenza: misurato **64 richieste/minuto accettate
in sequenza, zero risposte 429, e 10 richieste concorrenti su 10 accettate**.

Il tetto ai creator non c'entra: quello limita il cron, questo è il percorso
manuale, e ogni richiesta accettata è una inferenza Gemini più una chiamata
Apify. Con concorrenza 10 e latenza 60s sono **600 analisi/ora**, cioè **$37 –
$175 all'ora** per singolo account a seconda del modello dietro
`gemini-flash-latest`.

Da affrontare prima del lancio a pagamento, insieme al resto dell'audit.

### ✅ RISOLTO — esecuzioni del cron sovrapposte, e il cron ora è spento per default

Chiuso il **10 agosto 2026**, migration `0005` + `job_locks`. Corrisponde alla
sezione 6 dell'audit di readiness.

**Prima però: il meccanismo non era quello che la voce originale descriveva.**
Il cron non è invocato da nessuno scheduler interno — nessun APScheduler, niente
fra le dipendenze — ed è un endpoint HTTP che **oggi nessuno chiama**:
`render.yaml` definisce solo `type: web`, `.github/workflows/` contiene solo
`backend-tests.yml`, e nessun workflow cron è mai esistito in storico. Il rischio
era quindi interamente prospettico.

**E la sorgente della sovrapposizione non era lo schedule.** La configurazione
documentata usa già `concurrency: picox-cron`, che accoda un secondo giro
schedulato. Era il **retry del client**: `curl --max-time 120 --retry 2`
abortiva e ri-POSTava se il censimento superava i 120s, mentre il server stava
ancora elaborando — stessa run, stesso gruppo di concorrenza, nessuna guardia. E
il censimento era sequenziale con `APIFY_TIMEOUT_SECONDS = 180`: al tetto di 30
creator attivi bastavano ~4s a creator per sfondare la soglia, cioè il
funzionamento normale di un account pieno.

**Baseline misurata**, due POST concorrenti con Apify mockato:

| Misura | Atteso | Prima | Dopo |
|---|---|---|---|
| `fetch_latest_videos` con 3 creator | 3 | **6** | **3** |
| Esecuzioni che saltano il giro | 1 | **0** | **1**, con `WARNING` |
| Picco di analisi in volo (limite 2) | ≤ 2 | **4** | **2** |
| Righe duplicate in `insights` | 0 | 0 | 0 |

L'ultima riga è ciò che ha guidato il design: `analysis_locks` della sezione 2
**proteggeva già** tutta la fase costosa a valle. Il danno residuo era confinato
al censimento e al semaforo.

**La correzione, in quattro pezzi.**

1. **`job_locks`** (`0005`): lock a scadenza con chiave `job_name`, stesso
   meccanismo a due passi atomici della `0003`. Chi arriva mentre un giro è in
   corso **salta** — `200` con `"skipped": true` e un `WARNING` — invece di
   attendere: un cron che si accoda è peggio di uno che perde un giro.
   La chiave è un nome di job e non "il cron" di proposito: un secondo job
   futuro passa il proprio nome, senza migration né moduli nuovi.
2. **Il lock copre anche il background.** `BackgroundTasks` disaccoppia la
   risposta dal lavoro: la `200` parte a fine censimento mentre le analisi
   proseguono per minuti. Il rilascio vive in `_esegui_e_rilascia`, non nella
   route. TTL 1800s — qui **non** è un vincolo di correttezza come nella `0003`
   ma il limite di quanto si accetta di restare fermi per un processo morto: sta
   sopra un giro realistico e sotto lo schedule di 6h di un fattore 12.
3. **Semaforo per event loop**, non per esecuzione: `CRON_MAX_CONCURRENT_ANALYSES`
   ora significa quello che il nome dice. Indicizzato sul loop e non in una
   variabile singola perché un `asyncio.Semaphore` si lega al loop al primo
   `await` — con un singleton globale il modulo diventa inutilizzabile da
   qualunque contesto che crei un loop proprio, ed è così che il difetto è
   emerso nei test.
4. **Censimento parallelo** (`CRON_CENSUS_CONCURRENCY = 6`): al tetto di 30
   creator il caso tipico scende da ~120s a ~20s e il pessimo da 5.400s a ~900s.
   È la leva strutturale — alzare solo il timeout avrebbe lasciato il censimento
   lineare nel numero di creator.

**`cron_config.md` corretto insieme al codice**: `--retry` rimosso (ritentare una
POST costosa e non idempotente era la causa prima), `--max-time` da 120 a 300,
gestione di `skipped` nello script, e l'avvertenza che il Render Cron Job non ha
un equivalente di `concurrency` — è il lock a garantire un giro per volta, non
lo scheduler.

**La guardia: `CRON_ENABLED`, spento per difetto.** Prima l'unica cosa che
impediva al cron di girare rotto era una nota in un file di documentazione. Ora
`check-updates` risponde `503 cron_disabled` finché non lo si accende
deliberatamente in `render.yaml`. La dipendenza è dichiarata **dopo**
`verify_cron_secret`: invertendole, chiunque senza segreto dedurrebbe lo stato
di configurazione dell'istanza dalla differenza fra `503` e `401`.

**Prove** (`tests/test_concorrenza_cron.py`, 10 test):

| Prova | Esito |
|---|---|
| Due giri sovrapposti | 1 solo censimento, 1 solo giro effettivo |
| Il salto è visibile | `WARNING` con "giro saltato", non `INFO` |
| Parallelismo | picco entro il limite configurato |
| Lock durante il background | un secondo giro lanciato **da dentro** l'analisi viene respinto |
| Rilascio a fine background | `job_locks` vuota dopo la risposta |
| Rilascio su giro a vuoto | `job_locks` vuota anche senza analisi da accodare |
| Crash a metà giro (lock scaduto) | il giro successivo riparte, con `WARNING` di sottrazione |
| Lock ancora valido (controllo) | il giro si ferma — prova che la scadenza è davvero valutata |
| `CRON_ENABLED=false` | `503 cron_disabled`, nessuno scraping, nessun lock preso |
| Segreto assente o errato + cron spento | `401`, mai `cron_disabled` |

**Scansione dei pattern gemelli: nessuno.** `cron.py` è l'unico endpoint che usa
`BackgroundTasks`; non esistono webhook, worker, `asyncio.create_task` o
scheduler interni. L'altro trigger ripetibile costoso è `analyze-video`, già
protetto da `analysis_locks`, il cui problema aperto è il rate limit (vettore A)
— che è un problema di volume, non di sovrapposizione.

> ✅ **La migration `0005` è applicata** dal **15 agosto 2026**. Era rimasta
> indietro per mesi — il cron è spento, quindi nulla lo segnalava — ed è stata
> applicata insieme alla `0010`, quando è emerso che mancava. Verificato dopo:
> RLS attivo, zero policy, nessun privilegio ad `anon` e `authenticated`.
> `CRON_ENABLED` può ora essere portato a `true` senza che i giri falliscano
> nell'acquisizione del lock.

### ~~⚠️ PRIORITÀ MEDIA — due esecuzioni del cron sovrapposte~~ → superata

Emersa dalla scansione dei pattern gemelli del 9 agosto 2026, **chiusa il 10
agosto** dalla voce qui sopra. Si conserva perché due sue affermazioni si sono
rivelate imprecise, e sapere *come* è utile quanto sapere che il difetto c'era:

* diceva «due scheduler, o un retry»: la misura ha mostrato che gli scheduler
  non c'entravano — nessuno chiamava l'endpoint, e la configurazione documentata
  accodava già i giri schedulati. Era **solo** il retry;
* la trattava come priorità media *attiva*, mentre il rischio era prospettico.

Restano valide e non modificate le osservazioni sui pattern **sani** verificati
allora, che il fix non ha toccato:

Pattern controllati e **risultati sani**: `creators.py:68` è un `insert` con
vincolo `UNIQUE` tradotto in `409`, non un last-writer-wins; `update_creator`
(`creators.py:112`) scrive solo i campi effettivamente inviati
(`changed_fields()` con `exclude_none`), quindi nessun campo retrocede per
omissione; `profiles` non viene mai scritta dal backend.

### ✅ RISOLTO — `503 database_unavailable` sulla card di anteprima del creator

**15 agosto 2026.** La card sotto la barra del link rispondeva
`503 database_unavailable` su ogni tentativo. Non era un difetto del codice
della PR #7: la migration `0010` **non era mai stata applicata**, quindi
`creator_validations` non esisteva. `_leggi_cache` è la **prima** cosa che fa
`validate_creator` (`creator_validation.py:457`), PostgREST rifiuta la SELECT su
una tabella assente, `translate_postgrest_error` non riconosce quel codice fra i
casi di dominio e cade nel ramo generico → `DatabaseError` → 503.

Era uno stallo di procedura, non un bug: il piano era applicare la `0010` *dopo*
la conferma visiva della card, ma la card non è visibile *prima* della `0010`.
Quando una migration introduce la tabella che un endpoint legge per prima,
l'ordine è obbligato.

Applicate entrambe le migration mancanti (`0005` e `0010`) al progetto
`jaimkiagtolxbkftjapx`. Verificato dopo: tre tabelle presenti, RLS attivo con
zero policy su tutte e tre, **nessun privilegio** ad `anon` o `authenticated`,
trigger `validation_events_enforce_quota` abilitato, e
`validation_limit_for_tier` che restituisce `free → 50`, `pro → 500`,
`null → 50`.

> Nota sull'ordine: `apply_migration` marca la versione con l'istante di
> applicazione, quindi nella tabella `supabase_migrations` la `0005` compare
> **dopo** la `0009`. È cosmetico e rispecchia ciò che è davvero successo.

### ✅ RISOLTO — un `503` transitorio di Gemini faceva fallire l'analisi, senza mai ritentare

**15 agosto 2026.** Emerso provando la prima analisi YouTube reale: `503
gemini_unavailable`. Il passthrough non c'entrava — **funziona**, ed è la prima
volta che viene verificato contro l'API vera e non contro `FakeGemini`.

**Cosa succedeva.** `gemini-flash-latest` restituisce a intermittenza:

> This model is currently experiencing high demand. Spikes in demand are usually
> temporary. Please try again later.

Misurato sullo stesso video con la configurazione esatta di produzione
(`file_data` YouTube + `video_metadata(fps)` + `response_schema` +
`media_resolution`): **5 successi su 6** su `gemini-flash-latest`, 3 su 3 su
`gemini-flash-lite-latest`. Le riuscite in 4–10s con schema valido.

**Il difetto era nostro.** `_generate_with_retry` ha un ciclo di due tentativi,
ma copre **solo** la risposta fuori schema: su `APIError` solleva subito. E
`google-genai` non ritenta di suo — senza `retry_options` il suo default è
`stop_after_attempt(1)`. Quindi un errore che Google stesso dichiara temporaneo
terminava l'analisi al primo colpo, circa **una volta su sei**. Sul percorso con
download era peggio: Apify e il trasferimento erano già stati pagati e si
buttavano via.

**La correzione.** `types.HttpRetryOptions` sul client che già costruiamo —
meccanismo della libreria, nessun pattern nuovo — con `gemini_retry_attempts`
(3) da `Settings`. Sta sul client e non attorno alla singola chiamata perché
così copre anche `files.upload` e `files.get`, che avevano lo stesso problema e
nessun ciclo attorno.

**Il vincolo che il retry tocca, e che è la parte interessante.** I due retry si
**moltiplicano**: ogni tentativo sullo schema è una `generate_content` che
internamente può ritentare 3 volte. Il caso peggiore di un'analisi passa da
1020s a **2220s**, e `analysis_lock_ttl_seconds` era 1200. Lasciarlo lì avrebbe
fatto scadere il lock durante analisi ancora vive, rimettendo in circolo la
doppia spesa che la migration `0003` esiste per chiudere — **in silenzio**.
Portato quindi a **2400s**. Il costo è che un processo morto tiene occupata
quella tripla per 40 minuti invece di 20; è un blocco per (utente, video,
modalità), non globale.

**Prova.** 5 test nuovi in `tests/test_timeout_gemini.py`, verificati per
falsificazione:

| Difetto reintrodotto | Esito |
|---|---|
| `retry_options` rimosso dal client | **3 test rossi** |
| `analysis_lock_ttl_seconds` riportato a 1200 | **1 test rosso**, con il conto nel messaggio |

L'invariante del lock è ora **codificata**, non solo scritta in prosa nel
commento: il test calcola il caso peggiore dai `Settings` reali e da
`_TENTATIVI_SCHEMA`, così chi alzerà un timeout a valle troverà un test rosso
invece di scoperchiare il lock senza accorgersene. Il `2` del ciclo è diventato
una costante proprio perché il calcolo non fosse una coincidenza.

> Nota emersa per strada: `gemini-2.5-flash` risponde ora
> `404 no longer available to new users`. Conferma la scelta di
> `gemini-flash-latest` già in `render.yaml`.

### ⚠️ APERTE — cinque voci non bloccanti dalla revisione di sicurezza della PR #7

Giro di `security-reviewer` sull'intero diff della PR #7
(`feature-creator-validation-and-scraper`, 48 file), **13 agosto 2026**. Il
verdetto è stato «nessun blocker di sicurezza»: nessuna credenziale hardcoded,
nessun IDOR, nessuna fuga cross-tenant di dati utente, nessun SSRF nuovo,
nessun XSS. Un solo blocker, di correttezza (`fetch_video()` interrogava
`channels.list`), più due MEDIUM sul prompt: **tutti e tre corretti** in
`6e3b523`.

Restano queste cinque, aperte **per scelta**. Ognuna riporta il rischio esatto e
non solo il nome, perché il nome da solo non basta a decidere se valga la pena
chiuderle — e perché fra sei mesi nessuno si ricorderà cosa volesse dire «N3».

**1. `creator_validations.checked_at` è un oracolo cross-tenant.**
`_leggi_cache` gira *prima* di `_consuma_quota` (`creator_validation.py:457` e
`:464`), quindi un cache hit non costa quota; e la risposta porta `checked_at`,
che per contratto è l'istante dell'ultima verifica **reale**, cioè quella fatta
da chiunque. Un utente autenticato può perciò enumerare handle a costo zero e
sapere, per ciascuno, **se e quando** un altro utente di Picox l'ha validato
nelle ultime 24h. Non scopre *chi*, e non legge alcun dato di quell'utente: il
contenuto della cache è già pubblico su Instagram. Ciò che trapela è il *fatto
della richiesta* — su una base utenti piccola, quali creator stanno guardando
gli altri. Chiuderla costa poco: arrotondare `checked_at` alla finestra del TTL,
o restituire una freschezza booleana. Se si accetta, va detto nella docstring
dell'endpoint, perché il commento a `creators.py:124-125` oggi giustifica la
cache condivisa col fatto che «il profilo non è un dato di nessuno» — vero del
contenuto, non del fatto che sia stato chiesto.

**2. Il passthrough del cron non riverifica l'host prima di dare l'URL a Gemini.**
Sul percorso manuale `youtube_url` è garantito su un host YouTube perché ci si
arriva solo dopo `detect_platform()`, che è un'allowlist di host esatti. Sul
percorso cron (`content_scraper.py:95-96`, da `cron.py`) l'URL è il campo `url`
di un item di dataset Apify: normalizzato, ma **mai ricontrollato** contro
quell'allowlist — la piattaforma viene dalla riga `creators`, non dall'URL. Se
un actor restituisse un URL su un host diverso, lo passeremmo a Gemini come
fosse YouTube. Non è un SSRF sulla nostra rete (a scaricare sarebbe Google) e
oggi non è raggiungibile, perché l'actor è scelto in base alla piattaforma. È
una guardia mancante in un punto dove il resto del modulo la applica ovunque.
Fix: aggiungere `and detect_platform(video.video_url) == "youtube_shorts"` alla
condizione.

**3. Il log di audit delle query non scopate ha perso valore di segnale.**
`unscoped_service_table` logga a INFO ogni accesso che bypassa il RLS
(`supabase_service.py:288`). Era progettato per essere **raro** — una riga per
giro di cron — così da poter notare un bypass scorrendo i log. Ora scatta a
**ogni** `POST /creators/validate`: una volta in lettura, e sui cache miss una
seconda in scrittura. Il rischio non è una vulnerabilità, è che la riga smetta
di funzionare come allarme: annegata nel traffico normale, un accesso non
scopato davvero anomalo non si distingue più. Fix: la cache a `debug`, oppure un
campo strutturato (`unscoped=True`) su cui filtrare.

**4. L'handle canonico di YouTube non ripassa dalla validazione dell'handle.**
`creator_validation.py:416` usa `canale.handle.lower()`, che è il `customUrl`
restituito da Google e **non** passa da `clean_username` / `_USERNAME_RE`. Il
frontend lo rimanda tale e quale a `POST /creators`, dove il backend lo rivalida
e risponderebbe **422**. Conseguenza concreta: se Google restituisse una stringa
fuori dal charset atteso, la card mostrerebbe un handle strano e il click su
«Segui» darebbe un errore inspiegabile — un flusso che si rompe a metà, non un
rifiuto onesto all'inizio. Non è né una fuga né un'iniezione: la chiave di
cache resta `target.identifier`, che è validato.

**5. `is_private` è fail-open sul campo che decide se un profilo è pubblico.**
`apify_service.py:296-298` fa `is_private=bool(privato)` con `privato = None`
quando **nessuna** delle chiavi `private` / `isPrivate` / `privateAccount` è
presente nel payload dell'actor. Se in una release l'actor rinominasse quel
campo, ogni profilo privato diventerebbe `is_public: true` **senza che nulla lo
segnali** — e `is_public` è esattamente ciò per cui questo endpoint esiste.
L'impatto è mite (un creator privato in watchlist non produce insight, quindi
non si spende) e la scelta è dichiarata nel commento, ma è l'unico punto della
PR in cui l'incertezza di un provider si risolve in senso permissivo: l'opposto
della regola applicata alla durata in `_enforce_duration(verificabile=False)`,
dove «non lo so» significa «non procedere».

> Una sesta voce, **non in questo elenco perché non è nuova**: sui cache hit e
> sui 422 di `parse_creator_input` non si consuma quota, e non esiste alcun
> middleware di rate limiting nell'app. Un utente autenticato può quindi
> generare traffico illimitato sul database da questo endpoint. È un problema di
> tutta l'API e non della PR — vedi «il vettore A resta aperto» qui sopra —
> ma `/creators/validate` è l'endpoint più economico da martellare, quindi
> quando si affronterà il rate limiting è da lì che conviene partire.

### TODO minori

Emersi dall'audit dei contratti, **nessuno bloccante**, tutti nel frontend:

1. ~~`frontend/components/search-bar.tsx` — l'input non ha `maxLength`~~ —
   **fatto** l'11 agosto 2026: `maxLength={200}`, lo stesso limite dichiarato dal
   backend con `Query(max_length=200)`.
2. ~~`frontend/components/creators-view.tsx` — la mutation di cancellazione
   invalida solo `["creators"]`~~ — **fatto** l'11 agosto 2026: invalida ora
   anche il prefisso `["insights"]`, che copre tutte le
   `["insights", { search, mode }]`.
3. `insights` non denormalizza l'handle del creator: cancellato il creator,
   l'attribuzione storica è persa per sempre. È coerente con `SET NULL`, ma se
   si vuole conservarla serve una colonna `creator_username` popolata alla
   scrittura. **Lasciato aperto per scelta** l'11 agosto: non è un difetto ma una
   conseguenza coerente di `ON DELETE SET NULL`, e aggiungere la colonna è una
   decisione di prodotto sulla conservazione dello storico.

Altro:
4. ~~Aggiungere `*.log` a `.gitignore`~~ — **fatto** l'8 agosto 2026, insieme
   alla cancellazione di `backend/backend.log`.
5. ~~`backend/.env.example` riporta ancora `GEMINI_MODEL=gemini-2.5-flash`~~ —
   **fatto** l'11 agosto 2026: allineato a `gemini-flash-latest`, il valore che
   `render.yaml` usava già. (Il `.env.example` di radice non contiene affatto
   quella riga: la nota precedente indicava il file sbagliato.)

8. **Normalizzazione degli URL incompleta** (review avversariale, verificato:
   9 URL dello stesso video → 9 righe → 9 inferenze). ~~Il caso peggiore è la
   porta: `media_service.py:114` usa `parts.netloc` invece di
   `parts.hostname`~~ — **corretto l'11 agosto 2026** (voce A8), con 12 test in
   `tests/test_normalizzazione_host.py`. Restano fuori, e sono la voce **A9**
   ancora aperta perché richiede una decisione, `youtu.be/<id>` vs
   `/shorts/<id>`, `instagram.com/{p,reel,reels}/<id>`, il punto finale
   dell'FQDN, il percent-encoding e i redirect brevi (`vm.tiktok.com`, `/t/`),
   questi ultimi già documentati nel codice.
9. **Copertura dei test su `media_service`.** La fixture `downloads` di
   `conftest.py` è `autouse` e sostituisce `download_to_temp` in tutta la
   suite: nessun test esercitava il modulo vero. L'8 agosto 2026 sono stati
   aggiunti test diretti su `_per_hop_headers`, sul logging del download e su
   `detect_platform`. `_assert_public_target` (la difesa SSRF) è stata coperta
   l'**11 agosto 2026** con 23 test in `tests/test_ssrf.py`. **Resta scoperta**
   `normalize_video_url` (la chiave di cache), deliberatamente: fissarne ora il
   comportamento vincolerebbe la decisione di A9.
   Nota collegata: `test_apify_non_raggiungibile_non_blocca_l_analisi`
   (`test_analyze_flow.py:360`) è verde per costruzione — con il codice reale
   l'URL originale di un Reel è HTML e `_guess_mime_type` solleverebbe 503. La
   proprietà "Apify non è fatale" vale solo per link diretti a `.mp4`.

### ✅ `backend/scripts/check_env.py` — due difetti, entrambi corretti

Emersi l'8 agosto 2026 usando lo script per validare la configurazione dopo la
rotazione delle chiavi, e lasciati fuori dal batch di fix pre-commit per
decisione esplicita. **Chiusi entrambi l'11 agosto 2026** (voce A11), con le
correzioni descritte sopra: caricamento dei due `.env` nell'ordine che riproduce
la precedenza di `config.py`, e riconfigurazione di **stdout** (non stderr, che
è il flusso usato dal pattern gemello in `block_frontend_secrets.py`).

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
