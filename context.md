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

## Prima di fidarti di questo file

L'ho ricostruito da `PROGRESS.md` (ora archiviato in
`docs/archive/PROGRESS-2026-08-15.md`) e `SECURITY_AUDIT.md`, che si sono
dimostrati non affidabili al 100% sulla recency; le contraddizioni che
avevano sono state riconciliate in `SECURITY_AUDIT.md` fra il 19 e il 22 agosto.
Verifica quanto sotto contro il repo vero prima di agire:

```bash
git log --oneline -20
git status
gh pr list
```

**`CRON_ENABLED` è `false` (`backend/render.yaml:73`), spento per scelta dal
22 agosto 2026 — non dimenticato.** Sul piano tecnico non manca nulla: PR sul
cron mergiata (`ee547a2`), migration `0005` applicata dal 15 agosto. Si è
deciso di non spendere finché la data di lancio non è fissata: il cron ha senso
solo con un deploy Render attivo, e quel deploy richiede una carta. Quando si
riprende, l'ordine è quello di `backend/app/cron_config.md` — prima lo
scheduler (passo 2, l'unico ancora da fare), **poi** `CRON_ENABLED=true`.
Accenderlo prima non avvia nulla: apre soltanto
`POST /api/v1/cron/check-updates` a chi ha `CRON_SECRET`.

**La migration `0011` (tetto globale di spesa) è applicata in produzione
(`jaimkiagtolxbkftjapx`) dal 22 agosto 2026 — non riapplicarla.** Verificata sul
database reale con utente usa e getta, 0 residui; il verbale sta nel commit e
nella migration stessa. `daily_cap_usd` è a `100.00`.

In particolare, ultimo stato noto ma **non riconfermato di recente**:

- Se `main` è allineato a `origin/main` — lo squash-merge locale non
  garantisce da solo che sia stato pushato.

## Task attivi

### A4 — rischio Sybil (più account)
**Parzialmente mitigata dal 22 agosto 2026**: l'opzione 4 (tetto globale di
spesa) è implementata dalla migration `0011` — `spend_limits` più il trigger
`enforce_global_spend_cap` su `analysis_events` e `validation_events`, che
solleva `PX004` → `409 global_capacity_reached`. Chiude la spesa, **non** la
registrazione di massa: chi crea abbastanza account può ancora esaurire il
tetto e fermare il servizio per tutti — un'indisponibilità, non un costo.

Restano da decidere le prime tre opzioni — dettaglio in `SECURITY_AUDIT.md`,
voce A4:

1. CAPTCHA al signup
2. Limite di registrazioni per IP o dominio email
3. Verifica della carta anche sul piano gratuito
4. Tetto globale di spesa lato Apify/Gemini, indipendente dal numero di account

**L'SMTP condiviso di Supabase permette solo 2 email/ora** (verificato il 23
agosto 2026 in dashboard, Authentication → Rate Limits: campo fisso, non
modificabile). È il vero vincolo di lancio, non un dettaglio dei test di oggi:
configurare un SMTP proprio è un **prerequisito**, non un'ottimizzazione.

E nello stesso momento in cui lo si fa, il CAPTCHA diventa l'**unica** barriera
Sybil residua, perché configurare l'SMTP toglie l'altra protezione di fatto —
quella quota — senza alcun segnale. Vedi A4 in `SECURITY_AUDIT.md`, e
`CLAUDE.md`.

### A13 — `docker compose up` mai verificato end-to-end
Bloccato da Docker non installato sulla macchina di sviluppo. Verifica
statica fatta e immagine pinnata (`python:3.11-slim-trixie`), ma build,
`ffmpeg`/`ffprobe` dentro il container e `/health` non sono mai stati provati
per davvero. Conta perché `render.yaml` con `runtime: docker` è l'unica cosa
che rende vero `MAX_VIDEO_DURATION_SECONDS` in produzione. ~10 minuti una
volta installato Docker.

### Cinque voci aperte per scelta dalla review di sicurezza della PR #7
Nessuna bloccante, nessuna richiede azione immediata — dettaglio completo in
`docs/archive/PROGRESS-2026-08-15.md`, sezione "APERTE — cinque voci non
bloccanti":
1. `creator_validations.checked_at` — oracolo cross-tenant a basso impatto
2. Il passthrough del cron non riverifica l'host prima di dare l'URL a Gemini
3. Il log di audit delle query non scopate ha perso valore di segnale (scatta
   troppo spesso ora, non è più raro come progettato)
4. L'handle canonico YouTube non ripassa dalla validazione dell'handle
5. `is_private` fail-open quando l'actor non espone il campo — **resta
   aperta**: `is_private=bool(privato)` (`apify_service.py:269`) è invariato
   dalla PR #7, e "l'actor non ne parla" vale ancora "pubblico". Il branch
   `blocca-follow-profili-privati` copre solo i profili *noti* come privati.

### A9, punto 2 — i link brevi (`vm.tiktok.com/...`) non sono risolti
Deliberato, priorità bassa: risolverli con una `HEAD` metterebbe una chiamata
di rete nel percorso della cache key. La via migliore — ri-chiavare dopo lo
scraping, che già risolve il link — tocca `perform_analysis` e il lock: è un
intervento a sé, non ancora pianificato.

### Confine del giorno delle quote — UTC per configurazione, non per codice
Deliberato, priorità bassa. I tre trigger di quota (`0008`, `0010`, `0011`)
usano `date_trunc('day', now())` per il confine del giorno — in pratica UTC
perché è il default della sessione Supabase, non perché il codice lo
garantisca. Allinearli esplicitamente a UTC è un lavoro a sé, va fatto ai tre
insieme se mai lo si fa: cambiarne uno solo creerebbe due "giorni" diversi
nello stesso database.

### Instagram non verificato per il parametro Apify mancante; YouTube ha un residuo di rischio sul ramo di fallback

`_single_video_input` passa `shouldDownloadVideos: true` solo per TikTok
(dove serviva: senza, l'actor non restituiva alcun URL scaricabile).

**Instagram passa sempre da lì** — `ApifyContentScraper` → `resolve_video`
→ `_single_video_input` (`content_scraper.py:198-199`): non ha un
passthrough, è l'unico percorso che ha. Non è mai stato controllato per lo
stesso problema, e riguarda quindi **tutto** il suo utilizzo, non un
sottoinsieme.

**YouTube passa di norma in passthrough** (`YouTubeContentScraper`,
`content_scraper.py:150-174`) e in quel caso non ha bisogno di alcun URL
scaricabile — non è a rischio lì. Ma ha anche un **ramo di fallback ad
Apify** quando la durata non è verificabile via passthrough
(`content_scraper.py:177-186`): è quel ramo, non tutto YouTube, a non
essere mai stato controllato per lo stesso problema di TikTok.

Corretto due volte dopo review sul codice reale: la prima nota (in
`PROGRESS.md`) includeva YouTube per intero come a rischio; la seconda
(qui, prima versione) invertiva la struttura — attribuiva a Instagram una
distinzione passthrough/fallback che appartiene solo a YouTube.

