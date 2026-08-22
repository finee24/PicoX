# BUG.md — Picox

Non è un tracker di stato: quello vive in `context.md` (cosa è aperto,
priorità, cosa manca). Qui vive il *ragionamento* dietro i bug che hanno
richiesto più di un'ipotesi prima di trovare la causa vera — per una sessione
futura che incontra un sintomo simile e vuole sapere "abbiamo già pensato a
questo?", non "cosa resta da fare".

Una voce qui non è mai solo "cos'era il bug": è soprattutto **quali strade
sembravano giuste e non lo erano**, e perché — altrimenti la prossima sessione
le riprova identiche da zero.

## Formato di una voce

```
### [sintomo riconoscibile — quello che si vede, non la causa]

**Quando/dove si è visto:** ...

**Ipotesi scartate, e perché:**
- ipotesi 1 — perché sembrava giusta, perché non lo era
- ipotesi 2 — idem

**Causa reale:** ...

**Fix:** breve. Pointer a commit/PR/migration se serve il dettaglio completo
— non ripetere qui uno spiegone che vive già altrove.
```

Il titolo è il **sintomo**, non la causa: è quello su cui una sessione futura
farà grep quando vede lo stesso errore.

---

## In corso

*(nessuna voce al momento. Quando un bug richiede più di un tentativo prima
della causa vera, la voce parte da qui — anche prima di aver trovato la
causa: "ipotesi scartate finora" è utile già a metà indagine, non solo a
fine indagine.)*

---

## Risolti

### `npm ci` fallisce in CI su "Missing: @emnapi/runtime / @emnapi/core from lock file"

**Quando/dove si è visto:** job "Build del frontend", dopo la PR #7. Su
`main` la CI passava — introdotto da un commit che aveva solo aggiunto
`react-icons`.

**Ipotesi scartate, e perché:**
- *Rigenerare il lockfile con npm 11* — sembrava la riparazione ovvia, ma
  npm 10 rifiuta comunque quel lock: npm 11 annida le voci `@emnapi` sotto
  `@tailwindcss/oxide-wasm32-wasi` invece di tenerle in cima, quindi npm 10
  le cerca dove npm 11 non le mette.
- *Ricostruire il lockfile da zero su Windows* — peggio: spoglia il lock di
  tutte le dipendenze delle altre piattaforme (−2905 righe, zero voci
  `@emnapi`).

**Causa reale:** `.nvmrc` fissa Node 22 → CI e Vercel usano npm 10; la
macchina di sviluppo gira su Node 24 → npm 11. Le due major registrano
diversamente le dipendenze opzionali transitive: `npm install react-icons`
sotto npm 11 aveva tolto dal lock due voci che npm 10 pretende.

**Fix:** partire dal lock di `main` e aggiungere *solo* `react-icons` con
npm 10 (+13 righe, nessuna rimozione) — verificato con
`npm@10 ci --dry-run --os=linux --cpu=x64`, non per somiglianza. Aggiunto
anche `frontend/.npmrc` con `engine-strict=true`: un `npm install` sulla
major sbagliata ora fallisce subito con `EBADENGINE` invece di scrivere in
silenzio un lockfile che si rompe solo in CI.

### `OverflowError` nel parsing di un contatore (follower/like/view) su YouTube

**Quando/dove si è visto:** durante la security-review della PR #8
(refactoring), sul lavoro di unificazione B4 — cinque parser di contatori
diversi (YouTube, Apify, ecc.) accorpati in uno solo.

**Ipotesi scartate, e perché:**
- Il refactoring era dichiarato "a comportamento invariato", e i test
  esistenti su input tipici (numeri normali) passavano identici prima e
  dopo — sembrava sufficiente per dire che nulla era cambiato.
- Perché non lo era: nessun test copriva input patologici (`Infinity`, apici
  tipografici come `"²"`), che i due parser originali gestivano già in modo
  diverso e comunque scorretto — uno restituiva `None` silenzioso, l'altro
  sollevava `OverflowError` — quindi "invariato" non voleva dire "corretto".

**Causa reale:** la versione unificata fa `isinstance(raw, (int, float))`
poi `int(raw)` diretto. `int(float('inf'))` solleva `OverflowError`;
`"²".isdigit()` restituisce `True` ma `int("²")` solleva comunque
`ValueError`.

**Fix:** guardia esplicita (valore finito) prima della conversione a
intero, con test che falliscono deliberatamente se la guardia viene
rimossa — la versione corretta è ora più sicura di entrambe le
implementazioni originali, non solo allineata a una delle due. Pointer:
commit sulla PR #8, sezione B4 della security-review.

> Ricostruita da memoria di conversazione, non dai documenti — verificala
> contro il commit vero (PR #8, sezione B4) prima di fidartene alla lettera.

### Analisi duplicate e `creator_id` che sparisce su `analyze-video`

**Quando/dove si è visto:** richieste concorrenti sullo stesso video
pagavano l'analisi più volte; separatamente, un insight con `creator_id`
già valorizzato tornava a `NULL` dopo una rianalisi.

**Ipotesi scartate, e perché:**
- *"È il cron a cancellare `creator_id` per mancanza di contesto"* — la nota
  precedente lo dava per assodato, ma verificato riga per riga è il
  contrario: il cron **passa sempre** `creator_id`; è il path manuale
  (`analyze-video` → `perform_analysis` senza quel parametro) a ometterlo.
- *"È una race condition"* — sembrava la spiegazione naturale per richieste
  concorrenti, ma lo scenario quotidiano che innesca il difetto è
  **sequenziale**: il cron analizza in `INFO`, l'utente più tardi richiede
  in `BOTH`, la riga non copre la modalità, si rianalizza e la riscrittura
  cancella il creator. Il fix `required_mode` sulla cache (8 agosto) aveva
  anzi **aumentato** l'esposizione: prima quasi ogni riga esistente era un
  cache hit e l'upsert non rigirava quasi mai.

**Causa reale:** nessuna coordinazione fra richieste che scrivono sulla
stessa riga di `insights` — né a livello di concorrenza né a livello di
payload (l'upsert riscriveva sempre `creator_id`, anche a `NULL`, quando il
chiamante non lo passava).

**Fix:** due garanzie separate. (1) Lock a TTL (`analysis_locks`) su
`(user_id, cache_key, analysis_mode)` — non più `video_url`: la migration
`0009` ha rinominato la colonna quando ha separato l'identità del video
(`cache_key`) dall'URL mostrato, e il lock doveva seguire, altrimenti
arbitrerebbe su una chiave diversa da quella che deduplica la cache,
riaprendo la stessa spesa doppia. (2) `_componi_payload_insight` (non
`_build_insight_payload` — nome corretto dopo verifica sul codice)
**omette** `creator_id` quando è `None`, invece di impostarlo
esplicitamente — indipendente dal lock, chiude anche il caso sequenziale.

Un cache hit da solo non basta: se la richiesta manuale (senza creator)
vince la corsa e scrive per prima, il cron la riceve poi come cache hit
senza mai attribuirla — e `_filter_already_analyzed` gli impedisce di
riprovare quel video. Da qui `_assicura_attribuzione`, che su un cache hit
aggancia il creator a una riga che ne è priva.

> Nota sui limiti di questa voce: l'affermazione che il fix `required_mode`
> (8 agosto) avesse aumentato l'esposizione a questo difetto è storica —
> verificato che il parametro esiste ed è usato nei lookup di cache, ma la
> dinamica passata non è verificabile dal codice attuale, né contraddetta
> da esso.

Pointer: `analysis_service.py:139` (composizione del payload, omissione
alle righe 195-196), `analysis_lock.py:47` (costruzione della chiave del
lock), migration `0003` e `0009` (quest'ultima per il rename della
chiave), `tests/test_concorrenza_analisi.py` (10 test).

### `503 gemini_unavailable` termina l'analisi al primo colpo

**Quando/dove si è visto:** prima analisi YouTube reale, circa 1 volta su 6.

**Ipotesi scartate, e perché:**
- *"È il passthrough YouTube ad avere un difetto"* — sospetto naturale visto
  dove si manifestava, ma il passthrough **funziona**: era la prima volta
  testato contro l'API vera invece che contro `FakeGemini`.
- *"`google-genai` ritenta da solo gli errori transitori"* — falso: senza
  `retry_options` il default della libreria è `stop_after_attempt(1)`.

**Causa reale:** `_generate_with_retry` aveva un ciclo di due tentativi che
copriva **solo** la risposta fuori schema — su `APIError` (l'errore "high
demand" che Google stesso dichiara temporaneo) sollevava subito. Misurato:
5/6 successi su `gemini-flash-latest`, 3/3 su `gemini-flash-lite-latest`.

**Fix:** `types.HttpRetryOptions` sul client (`gemini_retry_attempts=3`),
che copre anche `files.upload`/`files.get`, non solo `generate_content`.

Vincolo scoperto per strada: i due retry si **moltiplicano** — il caso
peggiore passa da 1020s a 2220s, sopra il vecchio
`analysis_lock_ttl_seconds` di 1200s. Portato a 2400s: altrimenti il lock
sarebbe scaduto durante analisi ancora vive, riaprendo in silenzio la
doppia spesa che la migration `0003` esiste per chiudere.

Pointer: `tests/test_timeout_gemini.py` (**7 test** — non 5, corretto dopo
verifica sul codice — falsificati rimuovendo `retry_options` o riportando
il TTL a 1200). `_TENTATIVI_SCHEMA = 2` sta in `gemini_service.py:87`; il
calcolo del caso peggiore (180+120+120+300×3×2 = 2220) è già scritto come
commento vicino a `analysis_lock_ttl_seconds` in `config.py`, non serve
rifarlo a mano.

### `check_env.py` non trova le variabili / crasha proprio quando c'è un errore da segnalare

**Quando/dove si è visto:** eseguito da `backend/` come da README, riportava
**tutte** le variabili obbligatorie come assenti anche con `.env` corretto;
su console Windows con cp1252, crashava (`UnicodeEncodeError`) invece di
mostrare l'errore che stava per segnalare.

**Ipotesi scartate, e perché:**
- *"Basta un `load_dotenv()` qualsiasi"* — incompleto: con `override=False`
  vince il **primo** file caricato, quindi l'ordine conta.
  `backend/.env` va caricato per primo per riprodurre la stessa precedenza
  di `config.py`; un `load_dotenv()` senza pensare all'ordine avrebbe
  riprodotto una precedenza diversa da quella reale dell'app.
- *"Basta applicare lo stesso fix di `block_frontend_secrets.py`"* — quel
  pattern riconfigura `stderr` per lo stesso tipo di crash, ma qui il
  report esce da `print`: il flusso giusto da riconfigurare è **stdout**,
  non `stderr`. Copiare il pattern per analogia diretta non avrebbe
  risolto nulla.

**Causa reale:** lo script leggeva solo `os.environ`, mai `.env` (dentro
Docker funzionava per caso, perché `env_file` popola l'ambiente prima
dell'entrypoint); e `Report.errore()`/`Report.ok()` stampano `✗`/`✓`, che
sollevano `UnicodeEncodeError` su una console cp1252.

**Fix:** `backend/.env` caricato prima del `.env` di root con
`override=False`; `python-dotenv` dichiarato esplicitamente in
`requirements.txt` (prima arrivava per fortuna, da una dipendenza di
pydantic-settings). Il flusso che serve riconfigurare è **stdout** — nel
codice si riconfigurano `stdout` **e** `stderr` in un ciclo, per sicurezza
(`check_env.py:44-46`).

Pointer: `backend/scripts/check_env.py:34-63` — il commento nel file è più
esteso dell'audit e non dipende dalla sorte dei documenti.

### Il riavvio di uvicorn su Windows non libera la porta

**Quando/dove si è visto:** dopo `taskkill`/`pkill` sul processo uvicorn, la
porta 8001 restava occupata — pur con i comandi che riportavano successo.

**Ipotesi scartate, e perché:**
- *"Se il comando dice che ha funzionato, ha funzionato"* — falso: uvicorn
  `--reload` crea un processo padre più un figlio; un kill parziale (solo
  uno dei due) lascia l'altro a tenere la porta, e il comando può comunque
  uscire con successo.

**Causa reale:** architettura padre/figlio di `uvicorn --reload`.

**Fix:** individuare esplicitamente entrambi i PID prima di terminarli:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*uvicorn*' } |
  Select-Object ProcessId, CommandLine
Stop-Process -Id <id1>,<id2> -Force
```

Pointer: nessuna migration — nota operativa, non un bug del codice
applicativo.

> La più debole delle voci qui sopra sul formato: l'unica "ipotesi
> scartata" è la fiducia nell'exit code di un comando, non un'ipotesi
> diagnostica vera e propria — resta più una procedura operativa che
> un'indagine. Se ricapita, cattura il testo esatto dell'errore di bind
> sulla porta 8001: accorcerebbe la diagnosi futura più di qualunque altra
> aggiunta qui.

---

### Un componente client non si idrata: nessun effetto parte, console pulita

**Quando/dove si è visto:** verifica manuale del widget Turnstile su
`/register`. Il markup del componente c'era (contenitore renderizzato dal
server), ma nessun `useEffect` girava: script mai iniettato, `window.turnstile`
`undefined`, contenitore vuoto. **Zero errori in console** — è la parte che
manda fuori strada.

Il sintomo era mascherato da una scelta di comodo: `/register` reindirizza a
`/dashboard` chi ha una sessione (`proxy.ts:65`), quindi per vedere il form
senza toccare i cookie di sessione avevo aperto la pagina dall'IP di rete
(`192.168.178.143:3000`) invece che da `localhost`.

**Ipotesi scartate, e perché:**
- *"`next/script` non inietta il tag"* — sembrava dimostrata: nessuno dei 18
  `<script>` in pagina puntava a challenges.cloudflare.com, e iniettando lo
  **stesso** URL a mano nella **stessa** pagina il widget compariva e produceva
  il token. Sembrava un esperimento controllato, e non lo era: l'iniezione
  manuale girava nella console, che non ha bisogno dell'idratazione, mentre il
  componente sì. Ho riscritto il componente su questa base — codice buttato.
- *CSP, sitekey, dominio* — esclusi dallo stesso esperimento: il widget
  renderizzava e restituiva il token, quindi nulla li stava bloccando.

**Causa reale:** Next dev **blocca le richieste cross-origin alle proprie
risorse** quando la pagina è servita da un host diverso da quello di sviluppo.
Aprendo da `192.168.178.143` i chunk `/_next/static/chunks/*` venivano
rifiutati, React non si idratava, e nessun effetto partiva. L'avviso non
compare in console del browser: sta **nel log del dev server**, ed è l'unico
posto dove guardare.

```
⚠ Blocked cross-origin request to Next.js dev resource /_next/static/chunks/...
Cross-origin access to Next.js dev resources is blocked by default for safety.
```

**Fix:** `allowedDevOrigins: ["192.168.178.143"]` in `next.config.ts`, **solo
per la durata della verifica**, poi rimosso — non è una modifica che serve al
prodotto. Con l'idratazione ripristinata **entrambe** le versioni funzionano, e
`next/script` è tornata quella buona.

La lezione riusabile non è su Turnstile: quando un componente client sembra
morto e la console è pulita, **il log del dev server è la prima cosa da
leggere**, non l'ultima. E un esperimento eseguito dalla console non prova nulla
sul codice del componente, perché salta esattamente il passaggio — l'idratazione
— che poteva essere rotto.

Pointer: PR sul branch `turnstile-signup`, `components/turnstile-widget.tsx`.
