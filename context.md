# CONTEXT.md — Picox

Stato attivo per chi riprende una sessione senza memoria della precedente. A
differenza di `claude.md` (regole stabili) e `bug.md` (ragionamento sui bug),
questo file cambia spesso — è tuo compito tenerlo aggiornato mentre lavori,
non solo leggerlo.

**Come mantenerlo:** quando un task si apre, aggiungilo. Quando si chiude: se
ha richiesto un'indagine con ipotesi scartate, sposta il ragionamento in
`bug.md` e qui lascia una riga sola; se è banale, cancella la voce e basta — il
commit o la PR portano già il dettaglio. Non lasciarlo diventare un secondo
PROGRESS.md: se una voce compie settimane senza essere toccata, va chiusa o
rivista, non solo riletta.

**Prima di aggiungere una voce, controlla la lunghezza** (metrica dell'hook:
`read_text` utf-8, poi `len`). Soglia operativa **8.000 caratteri**: oltre,
comprimi o togli qualcosa di equivalente **nella stessa modifica**.

Il tetto vero è **9.000** (`session_start_context.py:34`), oltre il quale
l'hook **tronca** e la sessione successiva riceve un file mutilato senza
saperlo. Se comprimere inizia a costare riferimenti `file:riga` invece di
ridondanze, il file non è lungo: ha una voce che andava chiusa, o una che sta
meglio come commento nel codice.

## Prima di fidarti di questo file

Ricostruito da `PROGRESS.md` (ora in `docs/archive/PROGRESS-2026-08-15.md`) e
`SECURITY_AUDIT.md`, non affidabili sulla recency. Verifica contro il repo
prima di agire: `git log --oneline -20`, `git status`, `gh pr list`.

**La migration `0011` (tetto globale di spesa) è applicata in produzione
(`jaimkiagtolxbkftjapx`) dal 22 agosto 2026 — non riapplicarla.** Il verbale sta
nel commit e nella migration. `daily_cap_usd` è a `100.00`.

## Prima del primo deploy

Quattro prerequisiti diventano bloccanti **insieme**, e nessuno si rimanda al
giorno dopo: `CRON_ENABLED` (manca solo lo scheduler — sezione qui sotto), un
**SMTP proprio** (necessario, ma toglie la protezione di fatto data dalla quota
email: vedi A4), una **sitekey Turnstile reale** del progetto Cloudflare, senza
la quale `next build` fallisce in produzione (PR #16), e **A13** se il deploy
usa `runtime: docker`: è l'unica cosa che rende reale
`MAX_VIDEO_DURATION_SECONDS`, e non è mai stato provato end-to-end.

## Prima di accendere il cron

**`CRON_ENABLED` è `false` (`backend/render.yaml:73`), spento per scelta dal 22
agosto 2026 — non dimenticato.** Non manca nulla: PR sul cron mergiata
(`ee547a2`), migration `0005` applicata dal 15 agosto. Non si spende finché la
data di lancio non è fissata. L'ordine è in `backend/app/cron_config.md` —
prima lo scheduler (passo 2, l'unico da fare), **poi** `CRON_ENABLED=true`.
Accenderlo prima non avvia nulla: apre solo l'endpoint a chi ha `CRON_SECRET`.

**Il tetto globale (`0011`) non copre il cron**: scrive fuori da
`analysis_events` per scelta della `0008` — `conta_quota=False` (`cron.py:198`)
e i trigger del tetto sono `before insert` su quella tabella (`0011:281-291`),
quindi nessuna riga, nessun controllo. Prima di accendere serve un tetto per il
cron o l'unione dei due budget: altrimenti gira senza limite globale. E il
**primo giro è il più caro**: il dedup non trova nulla di già analizzato e
accoda fino a 10 analisi per creator attivo (`apify_results_per_creator`).

## Task attivi

### A4 — rischio Sybil (più account)
**Due su quattro fatte** — dettaglio in `SECURITY_AUDIT.md`, A4:

1. ~~CAPTCHA al signup~~ — **fatta** (24 agosto), ma provata **solo sul
   signup**: il login era rotto, chiuso dalla PR #15. Chiavi di test: vedi i
   prerequisiti di deploy.
2. Limite di registrazioni per IP o dominio email — aperta
3. Verifica della carta anche sul piano gratuito — aperta
4. ~~Tetto globale di spesa~~ — **fatta** (22 agosto): `0011`, `PX004` → `409
   global_capacity_reached`. Chiude la spesa, **non** la registrazione di
   massa: chi crea abbastanza account esaurisce il tetto e ferma il servizio.

**L'SMTP condiviso di Supabase permette solo 2 email/ora** (campo fisso in
dashboard, non modificabile). Configurarne uno proprio toglie quella barriera
senza alcun segnale, lasciando il solo CAPTCHA.

### A13 — `docker compose up` mai verificato end-to-end
Bloccato da Docker non installato in locale, ~10 minuti una volta che c'è.
Verifica statica fatta e immagine pinnata (`python:3.11-slim-trixie`), ma
build, `ffmpeg`/`ffprobe` nel container e `/health` non sono mai stati provati.
È il quarto prerequisito di deploy qui sopra.

### `is_private` fail-open quando l'actor non espone il campo
`is_private=bool(privato)` (`apify_service.py:269`) legge un campo **assente**
come «non privato», ed è invariato dalla PR #7. La PR #13 (`5878c75`) ha chiuso
solo il caso *già noto* come privato — blocco nell'interfaccia e guardia su
`POST /creators` che legge la cache — non questo.

### La chiave della riga di cache delle validazioni
`_scrivi_cache` indicizza la riga sulla forma **cercata**
(`creator_validation.py:317`), mentre la risposta porta quella **canonica**: la
PR #14 ha chiuso la validazione del `customUrl`, non la divergenza fra le due
chiavi. Finché resta, due forme dello stesso canale costano due righe di cache
e due unità di quota. Ridisegno a sé: tocca `_scrivi_cache`, `_leggi_cache`,
`cached_validation` e la dedup.

### A9, punto 2 — i link brevi (`vm.tiktok.com/...`) non sono risolti
Deliberato, priorità bassa: risolverli con una `HEAD` metterebbe una chiamata di
rete nel percorso della cache key. La via migliore — ri-chiavare dopo lo
scraping, che il link lo risolve già — tocca `perform_analysis` e il lock:
intervento a sé.

### Confine del giorno delle quote — UTC di fatto, non per codice
Deliberato, priorità bassa. I tre trigger di quota (`0008`, `0010`, `0011`)
usano `date_trunc('day', now())`: UTC perché è il default della sessione
Supabase, **non perché il codice lo garantisca**. Se mai li si allinea, vanno
fatti tutti e tre insieme — uno solo creerebbe due "giorni" nello stesso
database.

### Il `503` di Gemini è intermittente, non legato al video
`@ingegneri_in_borsa/video/7677930225237314849` aveva **cinque
`503 gemini_unavailable` su cinque** (26-27 agosto) e sembrava un problema
di quel file. **Non lo è**: isolando Gemini da Apify e da `perform_analysis` —
solo upload e `generate_content` — è stato analizzato in **63,8s** senza un
retry.

Escluse **dimensione e proprietà del file**: il controllo che funziona è più
**grande** (18,84 MB / 154s contro 11,28 MB / 86s). Il `503` è
**intermittente**: lo stesso script, sugli stessi file, ne ha preso uno **dieci
minuti prima** di riuscire, senza modifiche.

**Non alzare `gemini_retry_attempts` per questo.** Il picco del 27 agosto è
durato oltre **251s**: anche 6 tentativi col backoff più largo restano a ~242s,
e già 4 sfora il budget del lock (`analysis_lock_ttl_seconds`). È la leva
sbagliata per la scala del guasto.

**Non spiegato** perché i cinque fallimenti si siano concentrati lì:
coincidenza di finestre o correlazione vera non sono distinguibili con le
osservazioni attuali. **Non isolata**: `perform_analysis` costruisce un
`AnalysisContext` che allunga il prompt, la prova isolata no.

**Nessun motivo per evitare di riprovare**: non c'è più un esito noto, c'è un
servizio intermittente. La prova si rifà con `GeminiService.analyze_video` su un
file scaricato, fuori da `perform_analysis`: non consuma quota.

Dallo stesso giro, non indagato: `POST /creators/validate` → **503** su
`@geopop` mentre l'analisi riusciva, senza righe nuove in
`creator_validations`. Se si ripresenta, merita una voce in `bug.md`.

### Il frontend non ha un test runner
`frontend/package.json` ha solo `dev`, `build`, `start`, `lint`. La logica di
`auth-form.tsx` — `isRegister`, le guardie sul captcha — non ha copertura
automatica: una regressione lì non la becca né `tsc` né `next build`, solo un
utente reale che non riesce a entrare. È esattamente come è nato il difetto
chiuso dalla PR #15.

