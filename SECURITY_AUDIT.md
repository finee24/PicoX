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

## Premessa: una sezione non è stata eseguita

**La sezione 3 (audit delle dipendenze — `npm audit` / `pip-audit`, vulnerabilità
high/critical rilevanti per il codice effettivamente eseguito) non è mai stata
svolta.**

Era in coda dopo la sezione 2 quando l'audit è stato interrotto per chiudere
subito il vettore B, e alla ripresa si è passati direttamente alla sezione 4.
Non è stata saltata per una decisione: è stata dimenticata nel cambio di
priorità. Compare in Categoria A come voce aperta (**A5**).

Questo documento è quindi completo su 6 sezioni su 7.

---

## Quadro di sintesi

| Sezione | Oggetto | Esito |
|---|---|---|
| 1 | Superficie di auth/autorizzazione, RLS riletto sullo schema reale | Chiusa — 2 difetti trovati e corretti (`profiles` scrivibile, `TRUNCATE` esente da RLS) |
| 2 | Abuso e rate limiting, con cifra in dollari | **Parziale** — vettore B chiuso, **vettore A aperto** |
| 3 | Audit delle dipendenze | **Mai eseguita** |
| 4 | Segreti sulla cronologia git completa | Chiusa — pulita, nessun segreto mai committato |
| 5 | Leak di informazioni negli errori | Chiusa — zero leak su 28 scenari; 1 finding minore |
| 6 | Esecuzioni sovrapposte del cron | Chiusa — corretta, **PR non ancora mergiata** |
| 7 | Readiness per il billing | Chiusa — raccomandazioni, nulla implementato |

---

# CATEGORIA A — debito già presente, indipendente dal billing

Va deciso ora, prima di aggiungere feature. Ogni voce porta due campi:

- **Pronta per un prompt diretto** — `no` significa che serve prima una tua
  scelta fra strade non equivalenti, elencate sotto la voce;
- **Dipendenze** — cosa andrebbe riscritto se un'altra voce venisse decisa in un
  certo modo. Servono a non correggere oggi qualcosa che domani va rifatto.

---

## A1 🔴 Auto-promozione a `pro` appena qualcuno concederà un GRANT su `profiles`

**Origine**: sezione 7, radice nella sezione 1 · **Stato**: aperto

La `0001` definisce `profiles_update_own`:

```sql
for update to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);
```

La policy consente di aggiornare **qualunque colonna** della propria riga,
`subscription_tier` compresa. Oggi non fa danno solo perché la `0002` ha revocato
il `GRANT`. Ma **RLS filtra righe, i GRANT filtrano colonne** — e la `0002` stessa
documenta come riaprire, per il caso legittimo in cui si vorrà far modificare
l'email:

```sql
grant update (email) on public.profiles to authenticated;
```

La forma naturale da digitare, il giorno in cui servirà, è
`grant update on public.profiles to authenticated` — senza lista di colonne. In
quel momento un `PATCH /rest/v1/profiles?id=eq.<il proprio uuid>` con
`{"subscription_tier":"pro"}` funziona, e il trigger della `0004` concede subito
200 creator attivi: **~$28/giorno di costo per account, gratuiti**.

Non è un bug attivo. È una mina posata su un percorso che verrà percorso.

**Pronta per un prompt diretto: NO.** Le strade non sono equivalenti:

- **Opzione 1** — spostare lo stato di pagamento in una tabella `subscriptions`
  separata, con zero privilegi di scrittura ad `authenticated`: la domanda «posso
  concedere l'update?» non si porrà mai sulla tabella che contiene il diritto di
  spendere.
- **Opzione 2** — lasciarlo in `profiles` e difenderlo sulla colonna:
  `revoke update (subscription_tier)` esplicito più un `WITH CHECK` che imponga
  la colonna invariata anche a GRANT concesso.
- **Opzione 3** — entrambe: spostare adesso, e mantenere comunque il `WITH CHECK`
  su `profiles` per le colonne sensibili che vi si aggiungeranno in futuro.

**Dipendenze**: **A2 dipende da questa scelta.** Con l'opzione 1, il trigger
`enforce_creator_limit` (`0004`) legge `public.profiles.subscription_tier` e va
riscritto per leggere dalla nuova tabella — insieme a ogni fix di A2 scritto
prima. Anche **A3** ne è toccata se la quota verrà parametrata sul piano.

---

## A2 🔴 Il downgrade non retroagisce: un ex-pagante continua a costare

**Origine**: sezione 7, derivata dalla `0004` della sezione 2 · **Stato**: aperto (latente)

`enforce_creator_limit` scatta solo su `INSERT` e sulla transizione `is_active`
false→true. Un cambio di `subscription_tier` non tocca `creators`, quindi nessun
trigger scatta; e `_load_active_creators` filtra su `is_active` senza guardare il
piano.

Un `pro` con 150 creator attivi che torna `free` (cap 30) **mantiene 150 creator
attivi**, e il cron continua a scraparli a ~$21/giorno per un utente che non paga
più.

È **latente** oggi: senza billing nessuno fa downgrade, se non con un `UPDATE`
manuale — che è però esattamente la procedura suggerita dalla `0004` per
concedere un'eccezione a un cliente.

**Pronta per un prompt diretto: NO.** È una decisione di prodotto prima che
tecnica:

- **Opzione 1** — al downgrade disattivare automaticamente i creator più recenti
  fino a rientrare nel cap.
- **Opzione 2** — rifiutare o sospendere il downgrade finché l'utente non scende
  da sé sotto il cap, notificandolo.
- **Opzione 3** — accettare il costo e limitarsi a renderlo visibile, senza alcun
  enforcement.

Non decidere significa scegliere la 3.

**Dipendenze**: da **A1** (dove vive il piano determina dove si aggancia il
trigger o il codice del downgrade). Da scrivere **dopo** A1, non prima.

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

**Dipendenze**: se la quota viene parametrata sul piano (probabile), dipende da
**A1**. Il conteggio per periodo è anche il presupposto di **B4** (misurare i
costi reali prima di fissare i prezzi): conviene che la stessa struttura serva a
entrambi.

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

## A5 🟠 L'audit delle dipendenze non è mai stato fatto

**Origine**: sezione 3 · **Stato**: **mai eseguito**

Nessun `npm audit`, nessun `pip-audit`, nessuna verifica di vulnerabilità note su
`requirements.txt` e `frontend/package-lock.json`. Non c'è alcun risultato: non è
"pulito", è **ignoto**.

Il perimetro concordato era: solo high/critical, e solo se rilevanti per il
codice effettivamente eseguito — un advisory su un pacchetto usato solo in build
non ha lo stesso peso di uno su un parser che tocca input dell'utente.

**Pronta per un prompt diretto: SÌ.** È un'esecuzione, non una scelta.

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
| A1 | Auto-promozione `subscription_tier` via futuro GRANT | 7 (radice 1) | A | aperto | **no** — 3 opzioni | basso–medio secondo l'opzione |
| A2 | Downgrade non retroattivo sui creator attivi | 7 (da 0004) | A | aperto (latente) | **no** — 3 opzioni | da stimare |
| A3 | Vettore A — nessun rate limit su `analyze-video` | 2 | A | aperto | **no** — 3 opzioni | medio |
| A4 | Rischio Sybil con più account | 2 | A | aperto | **no** — 4 opzioni | da stimare |
| A5 | Audit delle dipendenze | 3 | A | **mai eseguito** | sì | basso |
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

1. **A5** — l'audit delle dipendenze, perché è l'unica voce di cui non si conosce
   nemmeno l'esito, e perché costa poco.
2. **A1** — decidere dove vive lo stato di pagamento. È il collo di bottiglia:
   **A2** e in parte **A3** non vanno scritte prima.
3. **A3** — il tetto di consumo sul percorso manuale. È il buco più grosso, e la
   struttura che serve è la stessa di **B4**.
4. **A2** — la politica di downgrade, una volta deciso A1.
5. **A8 + A10 (parte SSRF) + A11** — correzioni brevi e indipendenti, buone da
   raggruppare in un solo giro.
6. **A9 + A10 (parte cache)** — dopo aver deciso quanto normalizzare.
7. **A4** — la risposta al Sybil, che conviene decidere insieme al pricing.
8. Solo allora la **Categoria B**, quando Stripe entrerà davvero.
