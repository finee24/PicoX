# SECURITY AUDIT — Picox

> Audit di readiness sicurezza e affidabilità, svolto fra il **9 e il 10 agosto
> 2026** prima dell'introduzione del billing. Sette sezioni concordate in
> apertura; questo documento le riassume tutte, ordinate per priorità.
>
> **Metodo.** Dove possibile le affermazioni sono misurate contro il sistema
> reale — backend in esecuzione, database di produzione in sola lettura, utenti
> usa e getta poi eliminati con verifica indipendente — e non dedotte dalla
> lettura del codice. Dove un numero è derivato invece che misurato, è detto.

---

## Nota di percorso

La **sezione 3** era stata inizialmente saltata: era in coda dopo la sezione 2
quando l'audit fu interrotto per chiudere subito il vettore B, e alla ripresa si
passò direttamente alla sezione 4. Non fu una decisione, fu una dimenticanza nel
cambio di priorità, ed è rimasta aperta come voce **A5** nella prima stesura di
questo documento.

**È stata eseguita il 10 agosto 2026** e il suo esito è ora in **A5**. Il
documento copre tutte e sette le sezioni.

---

## Quadro di sintesi

| Sezione | Oggetto | Esito |
|---|---|---|
| 1 | Superficie di auth/autorizzazione, RLS riletto sullo schema reale | Chiusa — 2 difetti trovati e corretti (`profiles` scrivibile, `TRUNCATE` esente da RLS) |
| 2 | Abuso e rate limiting, con cifra in dollari | Chiusa — vettore B (`0004`) e vettore A (`0008`) entrambi chiusi |
| 3 | Audit delle dipendenze | Chiusa — 0 vulnerabilità in produzione, 1 solo dev |
| 4 | Segreti sulla cronologia git completa | Chiusa — pulita, nessun segreto mai committato |
| 5 | Leak di informazioni negli errori | Chiusa — zero leak su 28 scenari; 1 finding minore |
| 6 | Esecuzioni sovrapposte del cron | Chiusa — corretta, **PR non ancora mergiata** |
| 7 | Readiness per il billing | Chiusa — A1 e A2 poi corrette (`0006`, `0007`); il resto resta raccomandazione |

---

# CATEGORIA A — debito già presente, indipendente dal billing

Va deciso ora, prima di aggiungere feature. Ogni voce porta due campi:

- **Pronta per un prompt diretto** — `no` significa che serve prima una tua
  scelta fra strade non equivalenti, elencate sotto la voce;
- **Dipendenze** — cosa andrebbe riscritto se un'altra voce venisse decisa in un
  certo modo. Servono a non correggere oggi qualcosa che domani va rifatto.

---

## A1 🟢 Auto-promozione a `pro` — **CHIUSA** (migration `0006`, 11 agosto 2026)

**Origine**: sezione 7, radice nella sezione 1 · **Stato**: **fatto**

### Il rischio era reale, ed è stato misurato prima di correggerlo

Non dedotto: riprodotto sul progetto, dentro una transazione poi annullata. Come
ruolo `authenticated`, con `grant update on public.profiles to authenticated` e i
claim JWT di un utente vero:

```
ruolo_effettivo: authenticated | tier_dopo_update: pro | auto_promozione_riuscita: true
```

Senza questo gruppo di controllo, la prova successiva avrebbe potuto essere verde
solo perché l'utente non poteva scrivere comunque.

### La correzione, e la prova che tiene

Lo stato di pagamento vive ora in `public.subscriptions`, e
`profiles.subscription_tier` **è stata rimossa**, non deprecata. Rieseguito lo
stesso attacco sullo schema applicato:

| Tentativo | Esito |
|---|---|
| `update profiles set subscription_tier` con `GRANT UPDATE` largo | **`42703`** — la colonna non esiste |
| `update subscriptions set tier` con `GRANT UPDATE` | **`42501`** — un `UPDATE … WHERE` richiede anche `SELECT`, non concesso |
| `update subscriptions set tier` con **`GRANT ALL`** | **0 righe viste, 0 toccate** — il RLS a zero policy filtra tutto |

L'ultimo caso è il più importante: anche concedendo *tutto* per errore, resta in
piedi un secondo strato indipendente. `tier` è rimasto `free`.

### Cosa contiene la `0006`

- `subscriptions` con FK verso **`auth.users`**, per coerenza con `creators`,
  `insights` e `analysis_locks`; passare da `profiles` accoppierebbe due tabelle
  applicative e aggiungerebbe un modo di fallire in cambio di nulla.
- Migrazione dati **idempotente** (`on conflict do nothing`): rieseguirla dopo
  che il billing avrà promosso qualcuno non lo retrocede. 2 righe migrate.
- `enforce_creator_limit` riscritta **prima** del `drop column`: i corpi PL/pgSQL
  non sono validati alla creazione ma all'esecuzione, quindi l'ordine inverso non
  sarebbe fallito lì — sarebbe fallito al primo insert di un creator, in
  produzione.
- **Nessun privilegio** ad `anon` e `authenticated`, nemmeno `SELECT`: verificato
  che oggi il piano non serva lato client. Le due righe per concederlo quando
  servirà sono scritte in fondo alla migration.

Verificato prima di rimuovere la colonna che **nessun codice la leggesse né la
scrivesse**: `git grep subscription_tier` su `backend/` e `frontend/` → zero
occorrenze.

### Da dove veniva

La `0001` definiva su `profiles` una policy di UPDATE che copriva l'intera riga
(`profiles_update_own`, `using`/`with check` sul solo `auth.uid() = id`). A
reggere era il solo `GRANT` revocato dalla `0002` — e la `0002` stessa documenta
come riaprirlo, per il caso legittimo in cui si vorrà far modificare l'email.
La forma naturale da digitare quel giorno,
`grant update on public.profiles to authenticated` senza lista di colonne, avrebbe
concesso 200 creator attivi a chiunque: ~$28 al giorno per account.

Delle tre strade valutate — tabella separata, difesa sulla singola colonna, o
entrambe — è stata scelta la **tabella separata**: difendere una colonna protegge
quella colonna, e la prossima colonna sensibile richiederebbe di ricordarsene,
senza che nulla segnali la dimenticanza. `subscriptions` non ha alcun motivo
legittimo di essere scrivibile da un client, quindi la domanda non si pone — e
non si porrà nemmeno per `status`, `current_period_end` e l'id cliente Stripe
della Categoria B.

**Dipendenze risolte**: A2 è stata scritta sopra questo schema, nello stesso
branch, e `enforce_creator_limit` legge ora da `subscriptions`. Anche **A3** ne
dipendeva ed è stata costruita sopra: `analysis_limit_for_tier` legge il piano
dalla stessa tabella.

---

## A2 🟢 Il downgrade non retroagiva — **CHIUSA** (migration `0007`, 11 agosto 2026)

**Origine**: sezione 7, derivata dalla `0004` della sezione 2 · **Stato**: **fatto**

`enforce_creator_limit` scattava solo su `INSERT` e sulla transizione `is_active`
false→true. Un cambio di piano non tocca `creators`, quindi nessun trigger
scattava; e `_load_active_creators` filtra su `is_active` senza guardare il piano.
Un `pro` con 150 creator attivi che tornava `free` (tetto 30) **ne manteneva 150
attivi**, e il cron continuava a scraparli a ~$21/giorno per un utente che non
paga più. Il tetto valeva per chi saliva, mai per chi scendeva.

Delle tre strade — disattivare l'eccedenza, rifiutare il downgrade, accettare il
costo — è stata scelta la **disattivazione automatica**. La terza è quella che si
sceglie non decidendo, e la paga l'azienda; la seconda tiene in ostaggio chi
vuole solo smettere di pagare.

### Il criterio: si mantengono i più vecchi

`order by created_at desc, id desc` — i più recenti escono per primi.

È spiegabile in una frase («mantieni i primi 30 che hai aggiunto»), e un
downgrade è un momento in cui ciò che succede deve essere anticipabile
dall'utente e raccontabile da chi fa supporto senza interrogare il database. Non
dipende da `insights`, l'unico segnale d'uso disponibile, che ha buchi reali:
`ON DELETE SET NULL` fa perdere l'attribuzione, e un creator aggiunto ieri
avrebbe zero insight — verrebbe scartato proprio mentre è la scelta più
deliberata. Lo spareggio su `id` non è decorativo: un inserimento in blocco
produce `created_at` identici, e senza spareggio due esecuzioni sugli stessi dati
disattiverebbero creator diversi.

**La disattivazione non è distruttiva**: riga e insight storici restano, e
l'utente può riattivare ciò che vuole disattivando altro. Il criterio non decide
cosa si perde, decide da dove si riparte.

### Prova end-to-end, su utente usa e getta poi eliminato

| Scenario | Esito |
|---|---|
| `pro` con **35** creator attivi → downgrade a `free` | **30 attivi, 5 disattivati** |
| Quali restano | `creator_01`…`creator_30` — **i più vecchi** |
| Quali escono | `creator_31`…`creator_35` — i più recenti |
| Righe conservate | **35 su 35** — nulla è stato cancellato |
| Upgrade `free` → `pro` | **nulla disattivato**, e nulla riattivato da sé: la scelta resta dell'utente |
| Riattivazione da `pro`, poi nuovo downgrade | il creator riattivato è di nuovo il primo a uscire — il criterio è stabile |

### Il tetto per piano vive ora in un posto solo

`creator_limit_for_tier(text)` è usata sia da `enforce_creator_limit` sia dal
trigger di downgrade. Senza, lo stesso `CASE` sarebbe esistito due volte: è
esattamente la divergenza segnalata come **B3** — il giorno in cui i piani
diventano tre, aggiornarne uno solo produce un database che concede ciò che
l'applicazione nega, senza che nulla fallisca.

Verificato in esercizio: `free → 30`, `pro → 200`, `null → 30`.

### Cosa il trigger non copre, deliberatamente

- **L'INSERT su `subscriptions`**: l'assenza della riga vale già `free`, il piano
  più basso — inserirne una può solo alzare il tetto.
- **Un futuro abbassamento dei numeri** in `creator_limit_for_tier`: nessun
  `UPDATE` su `subscriptions` avviene, quindi nessun trigger scatta. La migration
  che li cambierà dovrà fare da sé il rientro delle righe esistenti — annotato
  dentro la funzione, perché chi cambierà quei numeri guarderà lì e non le note
  di rilascio.
- **La riattivazione oltre il tetto**: già coperta da `enforce_creator_limit`,
  che **non interferisce** — il suo guard esce subito quando si sta disattivando.
  Una funzione sorveglia chi sale, l'altra chi scende.

---

## A3 🟢 Vettore A — **CHIUSA** (migration `0008`, 11 agosto 2026)

**Origine**: sezione 2 · **Stato**: **fatto**

### La correzione

Tabella append-only `analysis_events` — una riga per analisi **avviata** — più il
trigger `enforce_analysis_quota` che rifiuta l'inserimento oltre il tetto del
giorno, con SQLSTATE `PX002` tradotto in `AnalysisQuotaError` (409
`analysis_quota_reached`), distinto da `plan_limit_reached` del tetto creator.

**Il punto in cui si consuma** è dentro il lock, dopo entrambi i controlli di
cache e subito prima di `_esegui_analisi`. Le due alternative sbagliano in
direzioni opposte: più in alto si conterebbero i cache hit, che non costano
nulla; più in basso non si conterebbero le analisi che pagano Apify e poi
falliscono su Gemini — **le stesse che non lasciano riga in `insights`**, ed è il
motivo per cui contare gli insight avrebbe sottostimato proprio l'abuso.

**Append-only e non un contatore aggregato**: l'incremento richiederebbe una RPC,
perché un upsert PostgREST non può esprimere `conteggio = conteggio + 1`, e
aprirebbe una terza via d'accesso al database accanto alle due dichiarate in
`supabase_service`. In più le righe grezze sono **il dato che serve a B4**.

**Quota solo sul percorso manuale**: il cron passa `conta_quota=False`, perché ha
già il proprio budget dal tetto ai creator attivi. Due budget indipendenti —
altrimenti un giro notturno azzererebbe le analisi possibili di giorno.

`analysis_limit_for_tier(text)` affianca `creator_limit_for_tier`: `free` 30 al
giorno (~$1,05), `pro` 300 (~$10,50), su una base di ~$0,035 per analisi. Sono
**segnaposto dichiarati**, derivati e non misurati — ed è `analysis_events` a
rendere possibile misurarli.

### Verifica: 18 prove, backend reale contro database reale

Gemini e Apify sostituiti da doppi, quindi il trigger è vero e la spesa no.

| Prova | Esito |
|---|---|
| 30ª analisi (ultima ammessa) | `201` |
| 31ª analisi | **`409 analysis_quota_reached`** |
| Dettagli interni nel corpo | **nessuno** (né `PX002`, né nomi di tabella o funzione) |
| Il rifiuto consuma quota? | **no** — resta a 30 |
| Cache hit a quota esaurita | **`200`**, contatore invariato |
| Analisi con Gemini KO | `503`, **nessun insight**, **ma quota consumata** |
| Cron a quota esaurita | procede, e **non** consuma la quota manuale |
| **5 richieste concorrenti, 1 solo posto** | **1×`201`, 4×`409`** |
| Piano `pro` oltre 30 | `201` |

La riga sulla concorrenza è quella che conta: l'arbitro è il trigger, non il
processo, quindi un controllo applicativo leggi-poi-scrivi non sarebbe bastato.

> **Un errore commesso durante la verifica, e la regola che ne è uscita.** Lo
> scenario sul cron è stato eseguito invocando il cron **vero** contro il
> database reale. Il cron enumera i creator attivi di *tutti* gli utenti — è il
> suo scopo — quindi ha scritto due insight fasulli nel feed di un utente reale.
> Rimossi per id espliciti e verificato che restasse solo la riga legittima;
> nessun costo, nessuna perdita di dati, nessuna quota consumata all'utente.
> La regola: **contro il database reale non si invoca mai il cron**, perché è
> l'unico endpoint il cui perimetro non è l'utente della richiesta. La proprietà
> che si voleva provare era peraltro già coperta da un test sul doppio.

### Cosa resta annotato

`analysis_events` cresce senza limite e va potata oltre una finestra di
ritenzione. Non è stato scritto un job: sarebbe il secondo pianificato, e oggi
non c'è nemmeno il primo (`CRON_ENABLED` è `false`). Il meccanismo però esiste
già — la `0005` ha reso `job_locks` generica **sul nome del job** esattamente per
questo caso.

### Da dove veniva

**Stato originale**: aperto, mai affrontato

Misurato: **64 richieste/minuto accettate in sequenza, zero risposte 429, 10
richieste concorrenti su 10 accettate**. Ogni richiesta accettata è un'inferenza
Gemini più una chiamata Apify: con concorrenza 10 e latenza 60 s sono ~600
analisi/ora, cioè **$37–$175 all'ora per singolo account** a seconda del modello
dietro `gemini-flash-latest`.

Il tetto ai creator della `0004` **non lo chiude**: quello limita il cron, questo
è il percorso manuale. È il più grosso buco aperto del sistema, e il billing lo
peggiora in due modi opposti — un `free` con carta rubata, e un `pro` in perfetta
buona fede che consuma ordini di grandezza più di quanto versa.

Delle tre strade valutate — limiter in-process, quota di periodo su Postgres,
entrambi — è stata scelta la **quota su Postgres**: un limiter in memoria vale
per istanza, si azzera a ogni deploy e non può esprimere un tetto legato al
piano. La variante che contava le righe di `insights` è stata scartata perché
**sottostima**: un'analisi che paga Apify e poi fallisce non lascia riga.

**Dipendenza risolta**: il tetto è parametrato sul piano e lo legge da
`public.subscriptions`, la tabella introdotta da A1.

---

## A4 🟠 Rischio Sybil — il tetto limita un account, non N account

**Origine**: sezione 2 · **Stato**: aperto, nessuna azione decisa

Chi registra N account ottiene N × 30 creator attivi e il costo torna a scalare
linearmente. L'unica barriera oggi è la verifica email di Supabase Auth, che alza
il costo dell'attacco ma non lo impedisce: gli indirizzi usa e getta sono
gratuiti e automatizzabili.

**Pronta per un prompt diretto: NO.**

- **Opzione 1** — CAPTCHA al signup.
- **Opzione 2** — limite di registrazioni per IP o per dominio email.
- **Opzione 3** — verifica della carta anche sul piano gratuito.
- **Opzione 4** — tetto globale di spesa lato Apify/Gemini, come rete di
  sicurezza indipendente dal numero di account.

L'opzione 4 è l'unica che protegge **anche** dagli scenari non previsti, ed è la
più economica da mettere: non richiede codice.

**Dipendenze**: **A3** la mitiga parzialmente (un tetto di consumo per account
riduce la resa di ogni account falso) ma non la chiude.

---

## A5 🟢 Audit delle dipendenze — eseguito, nessun rischio di produzione

**Origine**: sezione 3 · **Stato**: **fatto** (10 agosto 2026)

Perimetro concordato: solo high/critical, e solo se rilevanti per il codice
**effettivamente eseguito** — un advisory su un pacchetto che non entra
nell'immagine non ha lo stesso peso di uno su un parser che tocca input
dell'utente.

### I numeri

| Superficie | Pacchetti | Vulnerabilità |
|---|---|---|
| Frontend, `npm audit` su `package-lock.json` | **753** (424 prod, 292 dev, 90 optional) | **0** |
| Backend **produzione**, `requirements.txt` risolto | **55** | **0** |
| Backend venv reale (produzione + sviluppo) | **67** | **1** |

Strumenti: `npm` 11.6.2 e `pip-audit` 2.10.1, quest'ultimo installato in un venv
usa e getta e rimosso a fine lavoro — **non è una dipendenza del progetto**.
Verificato dopo l'esecuzione che né `package-lock.json` né i `requirements` siano
stati modificati: `npm audit` e `pip-audit` sono di sola lettura, ma su un
lockfile appena riallineato per un problema di drift valeva la pena controllarlo
invece di darlo per scontato.

### Control group

"Zero vulnerabilità" e "lo strumento non ha interrogato nulla" producono lo
stesso output. Entrambi gli strumenti sono stati messi alla prova su versioni
notoriamente vulnerabili:

| Strumento | Esca | Rilevate |
|---|---|---|
| `npm audit` | `lodash@4.17.15`, `minimist@1.2.0` | 1 high + 1 critical |
| `pip-audit` | `jinja2==2.10` | 6 |
| `pip-audit` | `urllib3==1.26.4` | 12 |
| `pip-audit` | `requests==2.19.0` | 23 |

### Cross-check con una seconda fonte

`pip-audit` interroga per difetto l'indice di PyPI. Rieseguito con `-s osv`
(Open Source Vulnerabilities, database indipendente): **stesso esito su entrambe
le superfici** — zero su `requirements.txt`, lo stesso unico finding sul venv.

### L'unico finding, e perché è fuori perimetro

**`pytest` 8.4.2** — `PYSEC-2026-1845`, alias `GHSA-6w46-j5rx-g56g` /
`CVE-2025-71176`. Su UNIX pytest usa directory con il pattern
`/tmp/pytest-of-{user}`, il che consente a un utente locale di causare un
denial-of-service o forse di scalare privilegi. Corretto in **9.0.3**.

Non è un rischio per Picox, per tre ragioni indipendenti:

1. **Non entra in produzione.** `pytest` sta solo in `requirements-dev.txt`
   (`pytest>=8.0,<9.0`), e il `Dockerfile` copia e installa **solo**
   `requirements.txt` (righe 25–26). L'immagine non lo contiene.
2. **Non si applica dove gira in locale.** Il pattern di path è specifico di
   UNIX; qui la suite gira su Windows.
3. **Non si applica dove gira in CI.** I job usano `ubuntu-latest`: runner
   effimeri e a tenant singolo, dove "un altro utente locale" non esiste.

Nessuna delle due fonti consultate fornisce un punteggio di severità per questo
advisory — i campi disponibili sono `id`, `aliases`, `description`,
`fix_versions`. Non ne invento uno: la valutazione qui sopra è sull'impatto
descritto, non su un numero.

> **Dettaglio da conoscere prima di dire "aggiorniamo e via":** la correzione è
> in 9.0.3, che è **fuori** dal vincolo `pytest>=8.0,<9.0`. Aggiornare richiede
> quindi di allargare deliberatamente il range e rieseguire la suite su un major
> nuovo di pytest — non è un bump automatico. Vista l'assenza di impatto, la
> raccomandazione è di **non farlo ora** e di lasciare che il vincolo si allarghi
> quando ci sarà un motivo indipendente.

**Azione richiesta: nessuna.**

**Dipendenze**: nessuna.

---

## A6 🟢 Un 500 fuori da `SafeRoute` senza header CORS — **CHIUSA** (11 agosto 2026)

**Origine**: sezione 5 · **Stato**: **fatto**

Lo stack è `ServerErrorMiddleware → RequestContextMiddleware → CORSMiddleware →
ExceptionMiddleware → router`. Un'eccezione dentro un router con
`route_class=SafeRoute` è gestita da `ExceptionMiddleware`, **dentro** al CORS →
la risposta esce con `Access-Control-Allow-Origin`. Un'eccezione in un endpoint
**senza** `SafeRoute` arriva a `ServerErrorMiddleware`, **fuori** dal CORS →
stessa identica risposta JSON, **senza** header CORS.

Non è un leak: la risposta è sanificata in entrambi i casi. È che il browser non
riesce a leggerla e il frontend mostra "errore di rete" invece dell'envelope. Il
caso non è teorico: `/health` in `main.py` è già registrato così.

**La correzione.** `_cors_headers(request)` in `error_handler.py`, usata da
`_handle_unexpected`: legge `Origin`, lo confronta **per uguaglianza esatta**
contro `settings.cors_origins` — la stessa lista di `CORSMiddleware`, non una
regola parallela che potrebbe divergere — e solo se combacia lo rimanda con
`Allow-Credentials` e `Vary: Origin`. `ServerErrorMiddleware` **non si è
spostato**: è la sua posizione esterna a garantire che nessun endpoint futuro
possa scavalcarlo. Sono gli header a scendere lì.

**Prove** (`tests/test_cors_errori.py`, 5 test):

| Prova | Esito |
|---|---|
| 500 senza `SafeRoute`, origin ammesso | header CORS presenti |
| 500 senza `SafeRoute`, **origin ostile** | **nessun header**, l'origin non compare da nessuna parte |
| Nessun header `Origin` (curl, scheduler) | nessun header inventato |
| 500 **con** `SafeRoute` (gruppo di controllo) | invariato, header da `CORSMiddleware` |
| Corpo in entrambi i percorsi | sanificato — regressione della sezione 5 |

Il secondo è quello che rende il fix sicuro invece che comodo: riflettere
l'`Origin` insieme a `Allow-Credentials: true` avrebbe trasformato la risposta
d'errore nel buco che il CORS ristretto chiude.

---

## A7 🟡 Il rifiuto della preflight CORS è fuori dall'envelope

**Origine**: sezione 5 · **Stato**: aperto — **raccomandazione: non correggere**

Una preflight da origin non ammesso risponde `400 text/plain "Disallowed CORS
origin"`, generato da Starlette, fuori dal formato `{"error": {...}}` usato
ovunque. Nessun dettaglio interno, solo un formato incoerente.

Il corpo di una preflight non viene mai mostrato né all'utente né al JavaScript:
il browser fallisce la preflight e basta. Il beneficio di uniformarlo è **zero**;
il costo è una sottoclasse di un middleware di sicurezza da rileggere a ogni
aggiornamento di Starlette.

**Decisione dell'11 agosto 2026: non si corregge.** Rivalutata insieme ad A6 e
confermata. Il corpo di una preflight non viene mai mostrato né all'utente né al
JavaScript — il browser fallisce la preflight e basta — quindi il beneficio è
esattamente zero, mentre il costo è una sottoclasse di `CORSMiddleware` da
rileggere a ogni aggiornamento di Starlette. **Voce chiusa per scelta, non per
omissione.**

---

## A8 🟢 La porta rompeva la chiave di cache — **CHIUSA** (11 agosto 2026)

**Origine**: sezione 1 / review avversariale · **Stato**: **fatto**

`media_service.py:114` usa `parts.netloc` invece di `parts.hostname`, quindi
`tiktok.com:443` produce una chiave di cache diversa da `tiktok.com` — stesso
video, due righe, **due inferenze pagate**. È invisibile all'utente.

**La correzione.** `parts.hostname`, che normalizza da sé minuscole, credenziali
e porta — e sostituisce anche lo `split` su `@` che stava lì prima. Perdere la
porta non fonde risorse distinte: l'URL canonico forza già lo schema a `https`,
quindi `http://x:8080` e `https://x` collassavano comunque, e `detect_platform`
ammette solo gli host delle tre piattaforme supportate.

**Nessuna sovrapposizione con A9**, verificata prima di scrivere: il punto finale
dell'FQDN, le forme di path per piattaforma, il percent-encoding e i redirect
brevi restano fuori — `hostname` non li tocca.

**Prove** (`tests/test_normalizzazione_host.py`, 12 test): `:443`, `:80`, `:8443`,
host maiuscolo, `www.`/`m.` con porta, credenziali nell'URL, e URL senza host.
Gruppo di controllo: col vecchio codice `netloc` dava `tiktok.com:443` contro
`tiktok.com`, cioè due chiavi e due inferenze pagate.

---

## A9 🟠 Canonicalizzazione degli URL per piattaforma, incompleta

**Origine**: sezione 1 / review avversariale · **Stato**: aperto

Misurato: **9 URL dello stesso video → 9 righe → 9 inferenze**. Oltre alla porta
(A8) restano fuori `youtu.be/<id>` vs `/shorts/<id>`,
`instagram.com/{p,reel,reels}/<id>`, il punto finale dell'FQDN, il
percent-encoding e i redirect brevi (`vm.tiktok.com`, `/t/`).

**Pronta per un prompt diretto: NO.** Quanto normalizzare è una scelta con un
rischio speculare: canonicalizzare troppo può **fondere video distinti** e
restituire a un utente l'analisi di un altro video.

- **Opzione 1** — solo le equivalenze certe e verificabili offline (forme di path
  della stessa piattaforma, FQDN, percent-encoding): nessuna rete, nessun rischio
  di collisione.
- **Opzione 2** — anche i redirect brevi, risolvendoli con una richiesta HEAD:
  copre più casi ma introduce una chiamata di rete nel percorso di cache, con i
  suoi timeout e i suoi fallimenti.
- **Opzione 3** — canonicalizzare sull'ID del video estratto per piattaforma
  invece che sull'URL: la forma più robusta, e la più invasiva da scrivere.

**Dipendenze**: **A10** — i test su `normalize_video_url` vanno scritti **dopo**
questa decisione, altrimenti fissano il comportamento che si vuole cambiare.

---

## A10 🟠 Copertura di `media_service` — **parte SSRF CHIUSA** (11 agosto 2026)

**Origine**: sezione 1 · **Stato**: parziale — SSRF fatto, `normalize_video_url` differito con A9

La fixture `downloads` di `conftest.py` è `autouse` e sostituisce
`download_to_temp` in tutta la suite: per molto tempo nessun test ha esercitato
il modulo vero. L'8 agosto sono stati aggiunti test su `_per_hop_headers`, sul
logging del download e su `detect_platform`, ma **restano scoperti**:

- `_assert_public_target` — la difesa **SSRF**, cioè il controllo che impedisce
  di far scaricare al backend un indirizzo interno. Zero test;
- `normalize_video_url` — la chiave di cache, cioè ciò che decide se si paga
  un'analisi due volte.

Voce collegata: `test_apify_non_raggiungibile_non_blocca_l_analisi`
(`test_analyze_flow.py:360`) **è verde per costruzione** — con il codice reale
l'URL originale di un Reel è HTML e `_guess_mime_type` solleverebbe 503. La
proprietà che il test dichiara di provare vale solo per link diretti a `.mp4`. Un
test che non può fallire non è copertura.

**Fatto: `tests/test_ssrf.py`, 23 test.** Nessuno tocca la rete —
`socket.getaddrinfo` è sostituito, i redirect passano da un `httpx.MockTransport`.

| Gruppo | Copertura |
|---|---|
| Indirizzi rifiutati | loopback, metadata cloud `169.254.169.254`, link-local, `10/8`, `172.16/12`, `192.168/16`, CGNAT, unspecified, multicast, e i corrispondenti IPv6 |
| Host pubblico | passa — gruppo di controllo, senza il quale i test sopra sarebbero verdi anche con una funzione che rifiuta tutto |
| **DNS con più record, uno solo interno** | rifiutato: è l'attacco che passerebbe se il controllo guardasse solo il primo indirizzo |
| Schemi | `file://`, `gopher://`, `ftp://`, `data:` rifiutati **prima** di risolvere |
| DNS irraggiungibile | `503` e non `422` — un guasto esterno non è un input non valido |
| **Redirect verso la rete interna** | fermato, e la richiesta all'indirizzo interno **non viene mai emessa** |
| Catena di redirect infinita | si ferma al tetto |
| Messaggio d'errore | non riporta né l'IP né l'host: sarebbe un oracolo per mappare la rete |

**Resta aperta** la parte su `normalize_video_url`, deliberatamente: fissare ora
il comportamento della chiave di cache vincolerebbe la decisione di **A9**, che
deve poter cambiare quanto si normalizza senza rischiare di fondere video
distinti.

---

## A11 🟢 `check_env.py` — **CHIUSA** (11 agosto 2026)

**Origine**: sezione 1 (note preesistenti) · **Stato**: **fatto**

1. **Non carica mai `.env`**: legge solo `os.environ`. Dentro Docker funziona,
   ma il README lo documenta come passo del setup locale non-Docker, dove riporta
   *tutte* le variabili obbligatorie come assenti.
2. **Crash su console Windows**: stampa `✗` e `✓`; con la console a cp1252 la
   `print` solleva `UnicodeEncodeError`. Lo script muore **proprio quando ha un
   errore da segnalare** — il caso peggiore per uno strumento diagnostico.
   `block_frontend_secrets.py` risolve lo stesso problema con
   `sys.stderr.reconfigure(encoding="utf-8")`.

**Correzione 1 — il caricamento.** Lo script carica ora `backend/.env` e poi il
`.env` di root, con `override=False`. **L'ordine non è casuale**: con
`override=False` vince il primo file caricato, quindi `backend/.env` va per
primo per riprodurre la precedenza dichiarata in `config.py`, e le variabili
d'ambiente vere continuano a vincere su entrambi. `python-dotenv` è stato
**dichiarato in `requirements.txt`**: arrivava comunque come dipendenza di
pydantic-settings, ma usarlo direttamente senza dichiararlo lo rendeva fortunato
invece che intenzionale.

**Correzione 2 — l'encoding.** Il pattern di `block_frontend_secrets.py`
riconfigura `stderr`; qui il report esce da `print`, quindi il flusso da
riconfigurare è **stdout** — l'adattamento era necessario. Riconfigurati
entrambi, con `errors="replace"` come rete finale.

**Prove.** Lo script eseguito dalla cartella `backend/` come documenta il README
riporta ora la configurazione come valida invece di dichiarare assenti tutte le
variabili obbligatorie. Con `PYTHONIOENCODING=cp1252`: il codice vecchio solleva
`UnicodeEncodeError: character maps to <undefined>`, quello nuovo esce `0` con
stderr vuoto.

---

## A12 🟢 TODO minori — **CHIUSI** (11 agosto 2026)

**Origine**: sezione 1 · **Stato**: **fatto** (3 corretti, 1 lasciato per scelta)

- ✅ `search-bar.tsx` — `maxLength={200}`, lo stesso limite che `list_insights`
  dichiara con `Query(max_length=200)`. Nota registrata: il backend tronca poi a
  100 caratteri per la ricerca vera (`_SEARCH_MAX_LENGTH`), quindi fra 100 e 200
  caratteri l'utente non riceve errori ma cerca solo sui primi 100 — comportamento
  preesistente, non toccato;
- ✅ `creators-view.tsx` — la mutation di cancellazione invalida ora anche il
  prefisso `["insights"]`. È il prefisso e non la chiave completa perché le query
  reali sono `["insights", { search, mode }]`, e invalidare il prefisso le copre
  tutte — come già faceva `analyze-input.tsx`;
- ✅ `backend/.env.example` allineato a `gemini-flash-latest`, il valore che
  `render.yaml` usava già: erano due default diversi fra sviluppo e produzione;
- ⏸️ `insights` **non** denormalizza l'handle del creator. Lasciato com'è: non è
  un difetto ma una conseguenza coerente di `ON DELETE SET NULL`, e aggiungere
  una colonna `creator_username` è una scelta di prodotto sulla conservazione
  dello storico, non una correzione.

Verificato prima di agire che tutte e quattro fossero ancora aperte.

---

## A13 🟢 `docker compose up` non è mai stato eseguito

**Origine**: sezione 1 (note preesistenti) · **Stato**: aperto — non verificato

Il file è scritto ma mai provato. Non è un finding di sicurezza: è una
configurazione dichiarata funzionante senza prova.

**Pronta per un prompt diretto: SÌ** (è una verifica, non un fix).

**Dipendenze**: nessuna.

---

## A14 ⏳ Azioni in sospeso sul lavoro già fatto

**Stato**: parziale — codice completo e verificato, non ancora in produzione

1. La PR **`fix-cron-overlap`** (commit `22ea530`) è pushata ma **non mergiata**:
   `main` è a `3992ab2`. Finché non è mergiata, il cron resta senza guardia — ma
   è irrilevante, perché non lo invoca nessuno.
2. La migration **`0005_job_locks.sql` non è applicata** al database.
3. **`CRON_ENABLED` è `false`** in `render.yaml`.

L'ordine per accendere il cron è vincolato: **merge → migration `0005` →
`CRON_ENABLED=true`**. Applicare la migration prima del merge è innocuo;
accendere `CRON_ENABLED` prima della migration fa fallire ogni giro
nell'acquisizione del lock.

Nota di contesto: **Render non risulta aver mai deployato** — `picox-api.onrender.com`
risponde 404 su `/health` e su `/`. Un servizio con nome diverso non sarebbe
rilevato da questa verifica.

---

# CATEGORIA B — per quando il billing esisterà davvero

**Nessuna azione ora.** Stripe non esiste, non c'è alcun endpoint webhook, non
c'è alcuna colonna di sottoscrizione. Queste voci non sono debito: sono il
capitolato di quando si comincerà.

---

## B1 — Webhook: firma verificata sul corpo grezzo, prima del parsing

**Origine**: sezione 7, finding 3

Il pattern corretto esiste già in casa: `verify_cron_secret` usa
`hmac.compare_digest` sui byte, con il commento che spiega perché non `==`. Per
Stripe servono due cose che quel pattern non copre:

- la firma va calcolata sul **corpo grezzo** (`await request.body()`), **prima**
  di qualunque deserializzazione. Se FastAPI parsa il JSON e poi lo si
  riserializza per verificare, i byte cambiano e la firma non torna mai;
- va verificata la **tolleranza temporale** (Stripe: 5 minuti sul timestamp
  dentro `Stripe-Signature`), altrimenti un evento legittimo catturato resta
  riutilizzabile per sempre.

Il segreto è lo **signing secret dell'endpoint**, non la API key: due valori
diversi, facili da confondere. L'endpoint sta fuori dal CORS e senza JWT — non
c'è un utente.

## B2 — Idempotenza: le consegne duplicate sono la norma, non l'anomalia

**Origine**: sezione 7, finding 4

Stripe ritenta fino a 3 giorni. Serve
`processed_webhook_events (event_id text primary key, processed_at timestamptz)`:
`INSERT` **prima** di agire, e se collide (`23505`) l'evento si scarta. È lo
stesso principio già applicato due volte in questo repo — `analysis_locks` e
`job_locks`: **l'arbitro è il database, non il processo**.

Qui però **niente TTL**: un evento processato resta processato per sempre. Serve
piuttosto una pulizia periodica oltre i 90 giorni — ed è esattamente il secondo
job pianificato per cui `job_locks` è stata resa generica sul nome: gli basterà
passare il proprio.

Attenzione all'ordine: **Stripe non garantisce la sequenza**. Un
`customer.subscription.updated` vecchio consegnato dopo uno nuovo retrocederebbe
il piano. Va confrontato il timestamp dell'oggetto, non applicato ciecamente
l'ultimo arrivato.

## B3 — Lo stato "pagato" non può restare una stringa senza scadenza

**Origine**: sezione 7, finding 6

Scritto `pro`, resta `pro` per sempre: non esistono `status` né
`current_period_end`. Servono almeno `active | past_due | canceled | incomplete`
e la fine del periodo, e la domanda «questo utente è pagante?» deve avere **una
sola implementazione**.

Oggi quella logica vive in SQL, dentro il trigger:
`case coalesce(piano,'free') when 'pro' then 200 else 30 end`. Quando i piani
saranno tre e ci sarà anche lo stato, quel `case` e il codice Python che deciderà
le quote **divergeranno** — è la classe di bug in cui il database concede ciò che
l'applicazione nega, o viceversa.

## B4 — Misurare i costi reali prima di fissare i prezzi

**Origine**: sezione 7, finding 7

Nessun contatore esiste. I numeri di questo audit — $0,140/giorno per creator
attivo, ~$4,20 al tetto `free`, ~$28 al tetto `pro` — sono **derivati da timeout
configurati e listini pubblici, non misurati sul consumo reale**. Fissare un
prezzo su una stima non verificata è il modo classico di vendere sotto costo
senza accorgersene.

Il `pro = 200` di `enforce_creator_limit` è dichiarato segnaposto nella migration
stessa: a ~$840/mese di costo per utente va deciso **contro il prezzo del piano**,
non ereditato.

**I dati ora esistono**: `analysis_events` (migration `0008`) registra una riga
per analisi avviata, con `user_id`, `video_url`, `analysis_mode` e `created_at`.
È nata per imporre la quota di A3, ma è anche esattamente la base di misura che
qui mancava — comprese le analisi fallite dopo aver speso, che `insights` non
vedrebbe.

### Cosa **non** serve

Un sistema di entitlement generico, la fatturazione custom, un portale di
gestione abbonamento: Stripe fa tutte e tre. Il lavoro vero è in **A1**, **A3**,
**B1** e **B2** — che sono di Picox, non di Stripe.

---

# Tabella riassuntiva

| # | Finding | Sezione | Cat. | Stato | Prompt diretto | Sforzo |
|---|---|---|---|---|---|---|
| A1 | Auto-promozione `subscription_tier` via futuro GRANT | 7 (radice 1) | A | **fatto** (`0006`) | n/d | — |
| A2 | Downgrade non retroattivo sui creator attivi | 7 (da 0004) | A | **fatto** (`0007`) | n/d | — |
| A3 | Vettore A — nessun rate limit su `analyze-video` | 2 | A | **fatto** (`0008`) | n/d | — |
| A4 | Rischio Sybil con più account | 2 | A | aperto | **no** — 4 opzioni | da stimare |
| A5 | Audit delle dipendenze | 3 | A | **fatto** — 0 in produzione, 1 solo dev fuori perimetro | n/d | — |
| A6 | 500 fuori da `SafeRoute` senza header CORS | 5 | A | **fatto** | n/d | — |
| A7 | Preflight CORS fuori dall'envelope | 5 | A | **chiusa per scelta**: non si corregge | n/d | — |
| A8 | `netloc` invece di `hostname` nella chiave di cache | 1 | A | **fatto** | n/d | — |
| A9 | Canonicalizzazione URL per piattaforma | 1 | A | aperto | **no** — 3 opzioni | medio |
| A10 | Nessun test su `_assert_public_target` (SSRF) e `normalize_video_url` | 1 | A | **SSRF fatto**; `normalize_video_url` differito con A9 | n/d | — |
| A11 | `check_env.py`: non carica `.env`, crasha su cp1252 | 1 | A | **fatto** | n/d | — |
| A12 | TODO minori frontend + `.env.example` disallineato | 1 | A | **fatto** (3 su 4; il quarto lasciato per scelta) | n/d | — |
| A13 | `docker compose up` mai eseguito | 1 | A | non verificato | sì | basso |
| A14 | PR cron da mergiare, `0005` da applicare, `CRON_ENABLED` | 6 | A | parziale | n/d — azione manuale | basso |
| — | Chiave `service_role` trapelata e ruotata | 1 | — | **fatto** | — | — |
| — | `profiles` scrivibile da `authenticated` (GRANT) | 1 | — | **fatto** (`0002`) | — | — |
| — | `TRUNCATE` esente da RLS su tutte le tabelle | 1 | — | **fatto** (`0002`) | — | — |
| — | Spesa duplicata su analisi concorrenti | 1 | — | **fatto** (`0003`) | — | — |
| — | `creator_id` azzerato dall'upsert | 1 | — | **fatto** | — | — |
| — | Vettore B — insert diretti illimitati su `creators` | 2 | — | **fatto** (`0004`) | — | — |
| — | `insights` scrivibile dal client | 2 | — | **fatto** (`0004`) | — | — |
| — | Segreti nella cronologia git | 4 | — | **fatto** — pulito | — | — |
| — | Leak di informazioni negli errori | 5 | — | **fatto** — zero leak su 28 scenari | — | — |
| — | Esecuzioni sovrapposte del cron | 6 | — | **fatto** — vedi A14 per il rilascio | — | — |
| B1 | Webhook: firma sul corpo grezzo + tolleranza temporale | 7 | B | futuro | n/d | da stimare |
| B2 | Idempotenza degli eventi + ordine non garantito | 7 | B | futuro | n/d | da stimare |
| B3 | Stato sottoscrizione con `status` e `current_period_end` | 7 | B | futuro | n/d | da stimare |
| B4 | Misurare i costi reali prima di prezzare | 7 | B | futuro | n/d | da stimare |

---

# Ordine consigliato

1. ~~**A5**~~ — **fatto** il 10 agosto 2026: nessuna azione ne è derivata.
2. ~~**A1**~~ e ~~**A2**~~ — **fatte** l'11 agosto 2026, migration `0006` e `0007`.
3. ~~**A3**~~ — **fatta** l'11 agosto 2026, migration `0008`.
4. ~~**A6 + A8 + A10 (parte SSRF) + A11 + A12**~~ — **fatte** l'11 agosto 2026 in
   un unico giro, con **A7** chiusa per decisione (non si corregge).
5. **A9 + A10 (parte cache)** — dopo aver deciso quanto normalizzare. Da
   affrontare insieme allo scraper.
6. **A4** — la risposta al Sybil, che conviene decidere insieme al pricing.
7. **A13** — verificare `docker compose up`, mai eseguito.
8. Solo allora la **Categoria B**, quando Stripe entrerà davvero.

Della Categoria A restano aperte **tre voci**: **A4** e **A9**, entrambe in
attesa di una decisione di prodotto, e **A13**, che è una verifica e non un fix.
Nessuna è rossa, e nessuna blocca lo sviluppo di feature nuove.
