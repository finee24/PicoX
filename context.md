# CONTEXT.md — Picox

Stato attivo per chi riprende una sessione senza memoria della precedente. A
differenza di `claude.md` (regole stabili) e `bug.md` (ragionamento sui bug),
questo file cambia spesso — è tuo compito tenerlo aggiornato mentre lavori,
non solo leggerlo.

**Come mantenerlo:** quando un task si apre, aggiungilo. Quando si chiude: se
ha richiesto un'indagine con ipotesi scartate, sposta il ragionamento in
`bug.md` e qui lascia una riga sola; se è banale, cancella la voce e basta —
il commit o la PR portano già il dettaglio. Non lasciarlo diventare un
secondo PROGRESS.md: se una voce qui compie settimane senza essere toccata,
è un segnale che va chiusa o rivista, non solo riletta.

**Prima di aggiungere una voce, controlla la lunghezza** (metrica dell'hook:
`read_text` utf-8, poi `len`). Se l'aggiunta porta sopra i 7.000 caratteri,
comprimi o togli qualcosa di equivalente **nella stessa modifica** — non
lasciare che il file scivoli sopra soglia e se ne accorga solo la sessione
successiva.

## Prima di fidarti di questo file

Ricostruito da `PROGRESS.md` (ora in `docs/archive/PROGRESS-2026-08-15.md`) e
`SECURITY_AUDIT.md`, che si sono dimostrati non affidabili sulla recency.
Verifica quanto sotto contro il repo vero prima di agire: `git log --oneline
-20`, `git status`, `gh pr list`.

**La migration `0011` (tetto globale di spesa) è applicata in produzione
(`jaimkiagtolxbkftjapx`) dal 22 agosto 2026 — non riapplicarla.** Il verbale
della verifica sta nel commit e nella migration. `daily_cap_usd` è a `100.00`.

## Prima del primo deploy

Quattro prerequisiti diventano bloccanti **insieme**, e nessuno si rimanda al
giorno dopo: `CRON_ENABLED` (i due prerequisiti sono fatti, manca lo scheduler —
sezione qui sotto), un **SMTP proprio** (necessario, ma toglie la protezione
data dalla quota email: vedi A4), una **sitekey Turnstile reale** del progetto
Cloudflare senza la quale `next build` fallisce in produzione (PR #16), e
**A13** se il deploy usa `runtime: docker` — quel runtime è l'unica cosa che
rende reale `MAX_VIDEO_DURATION_SECONDS`, e `docker compose up` non è mai stato
verificato end-to-end.

## Prima di accendere il cron

**`CRON_ENABLED` è `false` (`backend/render.yaml:73`), spento per scelta dal
22 agosto 2026 — non dimenticato.** Tecnicamente non manca nulla: PR sul cron
mergiata (`ee547a2`), migration `0005` applicata dal 15 agosto. Non si spende
finché la data di lancio non è fissata. Quando si riprende, l'ordine è quello di
`backend/app/cron_config.md` — prima lo scheduler (passo 2, l'unico da fare),
**poi** `CRON_ENABLED=true`. Accenderlo prima non avvia nulla: apre solo
l'endpoint a chi ha `CRON_SECRET`.

**Il tetto globale (`0011`) non copre il cron**: scrive fuori da
`analysis_events` per scelta della `0008` — `conta_quota=False` (`cron.py:198`)
e i trigger del tetto sono `before insert` su quella tabella (`0011:281-291`),
quindi nessuna riga, nessun controllo. Prima di accendere serve un tetto
dedicato al cron o l'unione dei due budget: altrimenti gira senza alcun limite
globale sopra. E il **primo giro è il più caro**, non un costo graduale: il
dedup non trova nulla di già analizzato e accoda fino a 10 analisi per creator
attivo (`apify_results_per_creator`).

## Task attivi

### A4 — rischio Sybil (più account)
**Due opzioni su quattro sono fatte** — dettaglio in `SECURITY_AUDIT.md`, A4:

1. ~~CAPTCHA al signup~~ — **fatta** (24 agosto), ma provata **solo sul
   signup**: il login era rotto, chiuso dalla PR #15. **In produzione servono
   chiavi reali**: ora ci sono quelle di test, che passano sempre.
2. Limite di registrazioni per IP o dominio email — aperta
3. Verifica della carta anche sul piano gratuito — aperta
4. ~~Tetto globale di spesa~~ — **fatta** (22 agosto): `0011`, `PX004` → `409
   global_capacity_reached`. Chiude la spesa, **non** la registrazione di massa:
   chi crea abbastanza account esaurisce il tetto e ferma il servizio.

**L'SMTP condiviso di Supabase permette solo 2 email/ora** (campo fisso in
dashboard, non modificabile). Configurarne uno proprio toglie l'altra barriera
Sybil di fatto — quella quota — senza alcun segnale, lasciando il solo CAPTCHA.

### A13 — `docker compose up` mai verificato end-to-end
Bloccato da Docker non installato sulla macchina di sviluppo, ~10 minuti una
volta che c'è. Verifica statica fatta e immagine pinnata
(`python:3.11-slim-trixie`), ma build, `ffmpeg`/`ffprobe` nel container e
`/health` non sono mai stati provati. Perché conta, vedi sopra.

### Review di sicurezza della PR #7 — due voci su cinque ancora aperte
**Tre chiuse dalla PR #12**; il dettaglio è nella PR. L'elenco originale sta
nell'archivio citato sopra, che le dà ancora tutte per aperte. Restano:

1. **`is_private` fail-open** quando l'actor non espone il campo:
   `is_private=bool(privato)` (`apify_service.py:269`) è invariato dalla PR #7.
   La PR #13 ha chiuso solo il caso *già noto come privato*; questo **resta
   aperto**.
2. **L'handle canonico YouTube** — la PR #14 ha chiuso la validazione del
   `customUrl`. Resta la **decisione 1**: sotto quale chiave scrivere la riga di
   cache (`creator_validation.py:317`). Ridisegno a sé — finché non si fa, due
   forme dello stesso canale sono due righe e due unità di quota.

### A9, punto 2 — i link brevi (`vm.tiktok.com/...`) non sono risolti
Deliberato, priorità bassa: una `HEAD` metterebbe una chiamata di rete nel
percorso della cache key. La via migliore — ri-chiavare dopo lo scraping, che il
link lo risolve già — tocca `perform_analysis` e il lock: intervento a sé.

### Confine del giorno delle quote — UTC per configurazione, non per codice
Deliberato, priorità bassa. I tre trigger di quota (`0008`, `0010`, `0011`) usano
`date_trunc('day', now())`: UTC perché è il default della sessione Supabase, non
perché il codice lo garantisca. Se mai li si allinea esplicitamente vanno fatti
tutti e tre insieme — cambiarne uno solo creerebbe due "giorni" nello stesso
database.

### Il parametro Apify mancante: Instagram, e il ramo di fallback YouTube

`_single_video_input` passa `shouldDownloadVideos: true` solo per TikTok (dove
serviva: senza, l'actor non restituiva alcun URL scaricabile). Mai controllato
per lo stesso problema: **tutto** Instagram, che di lì passa sempre e non ha
passthrough (`content_scraper.py:198-199`), e il **solo ramo di fallback ad
Apify** di YouTube (`:177-186`) — non tutto YouTube, che di norma va in
passthrough (`:150-174`), dove nessun URL scaricabile gli serve.

Già corretta due volte: la distinzione passthrough/fallback è solo di YouTube,
non riattribuirla a Instagram.

### Il frontend non ha un test runner
`frontend/package.json` ha solo `dev`, `build`, `start`, `lint`. La logica di
`auth-form.tsx` — `isRegister`, le guardie sul captcha — non ha copertura
automatica: una regressione lì non la becca né `tsc` né `next build`, solo un
utente reale che non riesce a entrare. È esattamente come è nato il difetto
chiuso dalla PR #15.

