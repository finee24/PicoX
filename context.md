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
dimostrati non affidabili al 100% sulla recency — vedi la sezione in fondo.
Verifica quanto sotto contro il repo vero prima di agire:

```bash
git log --oneline -20
git status
gh pr list
```

**Corretto il 18 agosto 2026**: la PR `fix-cron-overlap` **è mergiata** —
commit `ee547a2` ("Fix cron overlap: un giro per volta"), dentro uno
squash-merge di tre branch impilati che portò allora `main` a `686ad58`; oggi
la punta è `e05398e`. Verificato sul repo il 19 agosto 2026: `ee547a2` è
antenato di `main` (`git merge-base --is-ancestor`).

**Verificato il 19 agosto 2026**: `CRON_ENABLED` è ancora `false`
(`backend/render.yaml:73`), ma entrambi i prerequisiti sono confermati fatti —
la PR è mergiata (`ee547a2`, presente in `main`) e la migration `0005` è
applicata dal 15 agosto. Resta solo accenderlo.

**La migration `0011` (tetto globale di spesa) è applicata** al progetto di
produzione `jaimkiagtolxbkftjapx` dal **22 agosto 2026** — non riapplicarla.
Verificata sul database reale con utente usa e getta, poi eliminato con 0
residui: la riga esattamente al tetto passa (la condizione è `>`, non `>=`),
quella successiva è rifiutata con `PX004`, analisi e validazioni contano nella
stessa somma, e sotto violazione simultanea arriva `PX002` — il limite
specifico dell'utente — non `PX004`. `daily_cap_usd` è a `100.00`.

Attenzione alla sequenza: il database ha il trigger **prima** che il codice che
traduce `PX004` sia su `main` (PR #10 aperta, non mergiata). Finché è così, un
`PX004` verrebbe tradotto dal backend nel ramo generico — `503
database_unavailable` invece di `409 global_capacity_reached`. Senza
conseguenze pratiche oggi, perché su Render non risulta alcun servizio
deployato, ma va saputo.

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

*(Nota: questo non è "vettore A" — quel nome, nei documenti sorgente, è già
usato per il rate limit su `analyze-video`, chiuso con la migration `0008`.
Sono due problemi diversi, entrambi originati dalla sezione 2 dell'audit; la
prima versione di questo file li aveva confusi.)*
1. CAPTCHA al signup
2. Limite di registrazioni per IP o dominio email
3. Verifica della carta anche sul piano gratuito
4. Tetto globale di spesa lato Apify/Gemini, indipendente dal numero di account

Attenzione: configurare un SMTP proprio su Supabase Auth **rimuove** la
protezione di fatto che c'è oggi (quota dell'SMTP condiviso), senza alcun
segnale — vedi anche `claude.md`.

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
   aperta**. Il branch `blocca-follow-profili-privati` blocca solo i profili
   *noti* come privati (UI `d8cd615`, backend `3131654`);
   `is_private=bool(privato)` (`apify_service.py:269`) è invariato dalla PR #7
   e "l'actor non ne parla" vale ancora "pubblico". La guardia backend ha un
   suo fail-open separato e deliberato: su cache miss lascia passare.

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

### Plugin da installare (richiede l'utente)
`/plugin install` per github, vercel, commit-commands, pr-review-toolkit
@claude-plugins-official — non automatizzabile da una sessione.
`security-guidance` risultava già abilitato. Da verificare se è stato fatto
nel frattempo.

## Prossimo passo consigliato (da SECURITY_AUDIT.md, da riconfermare)

1. A13 — sbloccare la verifica di Docker
2. A4 — decidere la risposta al Sybil, insieme al pricing
3. Le cinque voci PR #7, quando c'è tempo
4. Categoria B (Stripe) solo quando il billing sarà davvero in cantiere
