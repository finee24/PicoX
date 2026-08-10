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
| 2 | Abuso e rate limiting, con cifra in dollari | **Parziale** — vettore B chiuso, **vettore A aperto** |
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

**Dipendenza risolta**: A2 è stata scritta sopra questo schema, nello stesso
branch, e `enforce_creator_limit` legge ora da `subscriptions`. **A3** resta
toccata: se la quota di periodo verrà parametrata sul piano, leggerà da qui.

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

## A3 🔴 Vettore A — nessun rate limit su `analyze-video`

**Origine**: sezione 2 · **Stato**: aperto, mai affrontato

Misurato: **64 richieste/minuto accettate in sequenza, zero risposte 429, 10
richieste concorrenti su 10 accettate**. Ogni richiesta accettata è un'inferenza
Gemini più una chiamata Apify: con concorrenza 10 e latenza 60 s sono ~600
analisi/ora, cioè **$37–$175 all'ora per singolo account** a seconda del modello
dietro `gemini-flash-latest`.

Il tetto ai creator della `0004` **non lo chiude**: quello limita il cron, questo
è il percorso manuale. È il più grosso buco aperto del sistema, e il billing lo
peggiora in due modi opposti — un `free` con carta rubata, e un `pro` in perfetta
buona fede che consuma ordini di grandezza più di quanto versa.

**Pronta per un prompt diretto: NO.**

- **Opzione 1** — rate limiter in-process per utente: semplice, ma vale per
  istanza e si azzera a ogni deploy.
- **Opzione 2** — quota per periodo su Postgres, contando `insights` per
  `user_id` e `created_at` (le colonne esistono già): funziona fra istanze e
  sopravvive ai riavvii, stesso principio di `analysis_locks` e `job_locks`.
- **Opzione 3** — entrambi: finestra breve in-process contro i burst, quota di
  periodo nel database contro l'abuso sostenuto.

**Dipendenze**: **A1 è chiusa**, quindi il blocco è rimosso — se la quota verrà
parametrata sul piano, lo leggerà da `public.subscriptions` tramite
`creator_limit_for_tier`, o da una funzione sorella costruita allo stesso modo.
Il conteggio per periodo resta il presupposto di **B4** (misurare i costi reali
prima di fissare i prezzi): conviene che la stessa struttura serva a entrambi.

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

## A6 🟡 Un 500 fuori da `SafeRoute` non porta gli header CORS

**Origine**: sezione 5 · **Stato**: aperto (minore, nessun leak)

Lo stack è `ServerErrorMiddleware → RequestContextMiddleware → CORSMiddleware →
ExceptionMiddleware → router`. Un'eccezione dentro un router con
`route_class=SafeRoute` è gestita da `ExceptionMiddleware`, **dentro** al CORS →
la risposta esce con `Access-Control-Allow-Origin`. Un'eccezione in un endpoint
**senza** `SafeRoute` arriva a `ServerErrorMiddleware`, **fuori** dal CORS →
stessa identica risposta JSON, **senza** header CORS.

Non è un leak: la risposta è sanificata in entrambi i casi. È che il browser non
riesce a leggerla e il frontend mostra "errore di rete" invece dell'envelope. Il
caso non è teorico: `/health` in `main.py` è già registrato così.

**Pronta per un prompt diretto: SÌ.** Strada già individuata e senza alternative
sensate: far generare gli header a `_handle_unexpected`, che riceve già la
`Request` — match **esatto** dell'`Origin` contro `settings.cors_origins` (la
stessa lista di `CORSMiddleware`, non una regola parallela), più
`Allow-Credentials` e `Vary: Origin`. Il punto delicato, da non sbagliare: **mai
riflettere l'`Origin` ricevuto senza verificarlo**, o la risposta d'errore
diventa il buco che il CORS ristretto chiudeva.

**Dipendenze**: nessuna.

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

**Pronta per un prompt diretto: SÌ** (tecnicamente banale) — ma la
raccomandazione è di lasciarlo com'è e considerare la voce chiusa per decisione.

**Dipendenze**: nessuna.

---

## A8 🟠 Normalizzazione URL: la porta rompe la chiave di cache

**Origine**: sezione 1 / review avversariale · **Stato**: aperto

`media_service.py:114` usa `parts.netloc` invece di `parts.hostname`, quindi
`tiktok.com:443` produce una chiave di cache diversa da `tiktok.com` — stesso
video, due righe, **due inferenze pagate**. È invisibile all'utente.

**Pronta per un prompt diretto: SÌ.** `netloc` → `hostname` non ha alternative
plausibili.

**Dipendenze**: va scritta insieme ai test di **A10**, o la correzione resta
senza copertura.

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

## A10 🟠 La difesa SSRF non ha alcun test

**Origine**: sezione 1 · **Stato**: parziale

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

**Pronta per un prompt diretto: PARZIALE.**
`_assert_public_target` **sì, subito** — il comportamento atteso non dipende da
nessuna decisione aperta, ed è la parte con rilevanza di sicurezza.
`normalize_video_url` **no**: dipende da A9.

**Dipendenze**: la parte su `normalize_video_url` dipende da **A9**; la parte
SSRF da nulla.

---

## A11 🟡 `check_env.py` — due difetti, uno dei quali si manifesta proprio quando serve

**Origine**: sezione 1 (note preesistenti) · **Stato**: aperto

1. **Non carica mai `.env`**: legge solo `os.environ`. Dentro Docker funziona,
   ma il README lo documenta come passo del setup locale non-Docker, dove riporta
   *tutte* le variabili obbligatorie come assenti.
2. **Crash su console Windows**: stampa `✗` e `✓`; con la console a cp1252 la
   `print` solleva `UnicodeEncodeError`. Lo script muore **proprio quando ha un
   errore da segnalare** — il caso peggiore per uno strumento diagnostico.
   `block_frontend_secrets.py` risolve lo stesso problema con
   `sys.stderr.reconfigure(encoding="utf-8")`.

**Pronta per un prompt diretto: SÌ.** Entrambe le correzioni sono meccaniche.

**Dipendenze**: nessuna.

---

## A12 🟢 TODO minori, tutti non bloccanti

**Origine**: sezione 1 · **Stato**: aperti

- `search-bar.tsx` — l'input non ha `maxLength`; oltre 200 caratteri il backend
  risponde 422 e l'utente vede un errore generico invece di un troncamento;
- `creators-view.tsx` — la mutation di cancellazione invalida `["creators"]` ma
  non `["insights"]`: la cache resta incoerente (effetto visivo nullo);
- `insights` non denormalizza l'handle del creator: cancellato il creator,
  l'attribuzione storica è persa. Coerente con `ON DELETE SET NULL`, ma serve una
  colonna `creator_username` se la si vuole conservare;
- `backend/.env.example` riporta ancora `GEMINI_MODEL=gemini-2.5-flash`, mentre
  `render.yaml` usa `gemini-flash-latest`.

**Pronta per un prompt diretto: SÌ.**

**Dipendenze**: nessuna.

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

Non serve infrastruttura nuova: `insights` ha `user_id` e `created_at`.

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
| A3 | Vettore A — nessun rate limit su `analyze-video` | 2 | A | aperto | **no** — 3 opzioni | medio |
| A4 | Rischio Sybil con più account | 2 | A | aperto | **no** — 4 opzioni | da stimare |
| A5 | Audit delle dipendenze | 3 | A | **fatto** — 0 in produzione, 1 solo dev fuori perimetro | n/d | — |
| A6 | 500 fuori da `SafeRoute` senza header CORS | 5 | A | aperto | sì | basso |
| A7 | Preflight CORS fuori dall'envelope | 5 | A | aperto | sì — *ma si raccomanda di non farlo* | basso |
| A8 | `netloc` invece di `hostname` nella chiave di cache | 1 | A | aperto | sì | basso |
| A9 | Canonicalizzazione URL per piattaforma | 1 | A | aperto | **no** — 3 opzioni | medio |
| A10 | Nessun test su `_assert_public_target` (SSRF) e `normalize_video_url` | 1 | A | parziale | parziale — SSRF sì, resto dopo A9 | basso–medio |
| A11 | `check_env.py`: non carica `.env`, crasha su cp1252 | 1 | A | aperto | sì | basso |
| A12 | TODO minori frontend + `.env.example` disallineato | 1 | A | aperti | sì | basso |
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
3. **A3** — il tetto di consumo sul percorso manuale. È ora il buco più grosso
   rimasto, e la struttura che serve è la stessa di **B4**.
4. **A8 + A10 (parte SSRF) + A11** — correzioni brevi e indipendenti, buone da
   raggruppare in un solo giro.
5. **A9 + A10 (parte cache)** — dopo aver deciso quanto normalizzare.
6. **A4** — la risposta al Sybil, che conviene decidere insieme al pricing.
7. Solo allora la **Categoria B**, quando Stripe entrerà davvero.

Chiuse A1, A2 e A5, la prima voce che richiede una tua decisione è ora **A3**.
