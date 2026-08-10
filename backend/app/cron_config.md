# Scheduling di `POST /api/v1/cron/check-updates`

Il backend non ha uno scheduler interno: espone un endpoint e si aspetta che
qualcuno lo chiami. È una scelta, non una mancanza — un processo che dorme
dentro il web server muore a ogni deploy, non lascia traccia di cosa ha fatto e
non è invocabile a mano quando serve.

## Prima di tutto: il cron è spento

`CRON_ENABLED` è `false` per difetto. Finché resta così, `check-updates`
risponde **`503 cron_disabled`** anche con il segreto corretto — non è un
guasto, è una guardia.

Esisteva solo una nota in questo file che diceva di non attivare lo scheduler.
Una nota non è una guardia: chi non la legge committa il workflow e scopre il
problema dai costi. Ora "il cron non è ancora pronto" è vero per il codice.

Da fare, in quest'ordine, prima di accenderlo:

1. applicare la migration `supabase/migrations/0005_job_locks.sql`, senza la
   quale ogni giro fallisce nell'acquisizione del lock;
2. configurare lo scheduler come descritto sotto;
3. **solo allora** portare `CRON_ENABLED` a `true` in `render.yaml`.

## Un giro per volta

Il backend rifiuta le esecuzioni sovrapposte da sé, con un lock a scadenza su
`job_locks` (`CRON_LOCK_TTL_SECONDS`, 1800s). Chi arriva mentre un altro giro è
in corso **salta**: risposta `200` con `"skipped": true`, contatori a zero, e un
`WARNING` nei log. Non è un errore e **non va ritentato** — accodare i giri è
peggio che perderne uno, perché la finestra successiva arriva comunque.

Il lock copre anche le analisi in background, non solo il censimento, e scade da
sé: un processo ucciso a metà deploy non blocca il giro successivo.

## Il contratto

```http
POST https://<backend>/api/v1/cron/check-updates
X-CRON-SECRET: <CRON_SECRET>
```

| | |
|---|---|
| **Autenticazione** | Solo l'header `X-CRON-SECRET`. Nessun JWT: non c'è un utente, il giro riguarda i creator di *tutti* gli account. Il confronto è a tempo costante (`hmac.compare_digest`). |
| **Risposta** | `200` con il riepilogo per creator, o `200` con `"skipped": true` se un altro giro è già in corso. `401` se il segreto manca o è errato. `503` se il database non risponde o se `CRON_ENABLED` è `false` (`cron_disabled`). |
| **Durata** | L'endpoint risponde appena finito il censimento; le analisi proseguono in background. Il censimento scrapa fino a `CRON_CENSUS_CONCURRENCY` creator in parallelo (6): al tetto di 30 creator attivi sono ~20s tipici, ~900s nel caso pessimo. Era sequenziale, e lì il caso tipico sfiorava i 120s. |
| **Idempotenza** | Sì, e su due livelli: il dedup è su `(user_id, video_url)`, e il lock su `job_locks` impedisce che due giri concorrenti scrapino gli stessi creator. Se un giro salta, il successivo recupera. |

Esempio di risposta:

```json
{
  "checked_creators": 12,
  "failed_creators": 1,
  "queued_analyses": 4,
  "results": [
    { "creator_id": "…", "username": "geopop", "platform": "tiktok",
      "status": "ok", "videos_found": 10, "new_videos": 2, "queued": 2 },
    { "creator_id": "…", "username": "altro", "platform": "instagram",
      "status": "failed", "error": "Il recupero dei video dal creator non è al momento disponibile." }
  ]
}
```

`failed_creators > 0` **non** è un fallimento del giro: Apify va in rate limit
con regolarità su un piano condiviso, e il job è costruito per isolare il
problema sul singolo creator e proseguire. Da monitorare è il caso in cui
`failed_creators == checked_creators`, che indica un problema globale (token
scaduto, Apify down) e non un creator sfortunato.

## Ogni quanto

Ogni 6 ore è un buon punto di partenza: i creator monitorati pubblicano al
massimo qualche volta al giorno, e ogni giro costa una chiamata Apify per
creator più un'analisi Gemini per ogni video nuovo. Aumentare la frequenza
aumenta il costo in modo lineare senza trovare più contenuti.

---

## Opzione consigliata — GitHub Actions

Funziona subito contro un backend su Render, non richiede codice aggiuntivo, e
lascia lo storico delle esecuzioni consultabile nel tab Actions.

**Questo workflow non è già nel repository di proposito**: una volta committato
parte a ogni schedule, e finché i due secret non esistono fallirebbe ogni sei
ore mandando una notifica ogni volta. Va aggiunto dopo aver configurato i
secret.

Creare `.github/workflows/cron-check-updates.yml`:

```yaml
name: Picox — controllo nuovi video

on:
  schedule:
    # Ogni 6 ore. GitHub usa UTC e mette in coda i job schedulati nei momenti
    # di picco: un minuto "strano" (17) viene servito prima di :00.
    - cron: "17 */6 * * *"
  # Permette di lanciarlo a mano dal tab Actions, utile per verificare la
  # configurazione senza aspettare la finestra successiva.
  workflow_dispatch:

# Se un giro precedente è ancora in corso non se ne accavalla un secondo.
concurrency:
  group: picox-cron
  cancel-in-progress: false

jobs:
  check-updates:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: Richiama l'endpoint di controllo
        env:
          BACKEND_URL: ${{ secrets.PICOX_BACKEND_URL }}
          CRON_SECRET: ${{ secrets.PICOX_CRON_SECRET }}
        run: |
          set -euo pipefail

          # Il segreto viaggia in un header, mai nella query string: gli URL
          # finiscono nei log di ogni proxy attraversato.
          # --fail-with-body: esce non-zero sugli status >= 400 mostrando però
          # il corpo, altrimenti l'errore arriva senza il motivo.
          #
          # NIENTE --retry SU QUESTA POST. Era `--retry 2 --retry-delay 30` con
          # `--max-time 120`, ed era la sorgente vera delle esecuzioni
          # sovrapposte: superati i 120s curl abortiva e ri-POSTava mentre il
          # server stava ancora elaborando la prima richiesta. Il `concurrency`
          # qui sopra non copre quel caso, perché avviene dentro la stessa run.
          # Oggi il lock lo intercetterebbe comunque, ma ritentare una POST
          # costosa e non idempotente resta la cosa sbagliata da fare: se un
          # giro fallisce, il successivo recupera.
          #
          # --max-time 300 e non 120: al tetto di 30 creator attivi un censimento
          # sano sta sotto i 30s, ma il caso pessimo — Apify lento su piu'
          # creator — arriva a ~900s. 300s abortisce solo quando c'e' davvero
          # qualcosa che non va, invece che a ogni giro un po' carico.
          curl --silent --show-error --fail-with-body \
               --max-time 300 \
               -X POST "${BACKEND_URL}/api/v1/cron/check-updates" \
               -H "X-CRON-SECRET: ${CRON_SECRET}" \
               -H "Content-Type: application/json" \
            | tee risposta.json

          echo "---"
          python3 -c "
          import json, sys
          d = json.load(open('risposta.json'))
          if d.get('skipped'):
              print('Giro saltato: un altro era gia in corso. Non e un errore.')
              sys.exit(0)
          print(f\"creator controllati: {d['checked_creators']}\")
          print(f\"analisi accodate:    {d['queued_analyses']}\")
          falliti = d['failed_creators']
          if falliti and falliti == d['checked_creators']:
              print('Tutti i creator falliti: problema globale, non del singolo account.')
              sys.exit(1)
          print(f'creator falliti:     {falliti}')
          "
```

Secret da creare in **Settings → Secrets and variables → Actions**:

| Secret | Valore |
|---|---|
| `PICOX_BACKEND_URL` | URL pubblico del backend, senza slash finale (es. `https://picox-api.onrender.com`) |
| `PICOX_CRON_SECRET` | Lo stesso valore di `CRON_SECRET` sul servizio Render |

> Su un repository privato con piano gratuito i minuti di Actions sono contati;
> questo job ne consuma pochi secondi per esecuzione. Su repository pubblico è
> gratuito. GitHub **disattiva i workflow schedulati** dopo 60 giorni di
> inattività del repository: se il progetto resta fermo, il cron si spegne in
> silenzio.

---

## Alternativa — Render Cron Job

Se il backend è già su Render, questa è la via con meno pezzi in movimento: il
segreto resta dentro Render, senza doverlo copiare su GitHub.

In `render.yaml`, come servizio aggiuntivo:

```yaml
  - type: cron
    name: picox-check-updates
    runtime: docker
    dockerfilePath: ./backend/Dockerfile
    dockerContext: ./backend
    region: frankfurt
    schedule: "17 */6 * * *"
    dockerCommand: >
      sh -c "curl --silent --show-error --fail-with-body --max-time 300
             -X POST \"$BACKEND_URL/api/v1/cron/check-updates\"
             -H \"X-CRON-SECRET: $CRON_SECRET\""
    envVars:
      - key: BACKEND_URL
        fromService:
          type: web
          name: picox-api
          property: host
      # Riusa lo stesso valore generato per il servizio web: nessuna copia
      # manuale, nessun rischio che i due divergano dopo una rotazione.
      - key: CRON_SECRET
        fromService:
          type: web
          name: picox-api
          envVarKey: CRON_SECRET
```

I Cron Job su Render sono a pagamento anche sui piani base — è il compromesso
in cambio dell'assenza di segreti duplicati.

⚠️ A differenza di GitHub Actions, qui **non c'è un equivalente documentato di
`concurrency`**: se un giro dura più dello schedule, il comportamento di Render
sulle esecuzioni accavallate non è qualcosa su cui contare. È il lock su
`job_locks` a garantire un giro per volta, non lo scheduler — motivo in più per
non trattarlo come una rete di sicurezza opzionale.

---

## Vercel Cron — serve un passaggio in più

Vercel Cron può invocare **solo path della stessa applicazione Vercel**. Non
può chiamare il backend su Render: il campo `path` di una voce `crons` è
relativo al deployment, non un URL arbitrario. Per usarlo serve quindi una
route nel frontend che faccia da tramite.

`frontend/app/api/cron/route.ts`:

```ts
import { NextResponse } from "next/server";

/**
 * Ponte fra Vercel Cron e il backend.
 *
 * Vercel firma le proprie invocazioni schedulate con
 * `Authorization: Bearer <CRON_SECRET>`, dove `CRON_SECRET` è una variabile
 * d'ambiente del progetto Vercel. Senza questo controllo la route sarebbe un
 * endpoint pubblico che chiunque può usare per far girare il job a ripetizione.
 */
export async function GET(request: Request) {
  const atteso = process.env.CRON_SECRET;
  if (!atteso || request.headers.get("authorization") !== `Bearer ${atteso}`) {
    return new NextResponse("Unauthorized", { status: 401 });
  }

  const risposta = await fetch(
    `${process.env.BACKEND_URL}/api/v1/cron/check-updates`,
    {
      method: "POST",
      // Il backend si aspetta il segreto in X-CRON-SECRET, non in Authorization.
      headers: { "X-CRON-SECRET": process.env.PICOX_CRON_SECRET ?? "" },
    },
  );

  return new NextResponse(await risposta.text(), {
    status: risposta.status,
    headers: { "Content-Type": "application/json" },
  });
}
```

E in `frontend/vercel.json`:

```json
{
  "crons": [{ "path": "/api/cron", "schedule": "17 */6 * * *" }]
}
```

Variabili d'ambiente del progetto Vercel — **senza** prefisso `NEXT_PUBLIC_`,
altrimenti finiscono nel bundle servito al browser:

| Variabile | Valore |
|---|---|
| `CRON_SECRET` | Segreto con cui Vercel firma le invocazioni schedulate |
| `BACKEND_URL` | URL del backend su Render |
| `PICOX_CRON_SECRET` | `CRON_SECRET` del backend (nome diverso per non collidere con quello di Vercel) |

Tre segreti e una route in più rispetto a un `curl` schedulato: ha senso solo se
si vuole un unico posto da cui gestire tutto. Il piano Hobby di Vercel consente
inoltre **una sola esecuzione cron al giorno**, che per questo caso d'uso è
troppo poco.

---

## Verifica manuale

```bash
# Deve rispondere 200 con il riepilogo
curl -i -X POST "$BACKEND_URL/api/v1/cron/check-updates" \
     -H "X-CRON-SECRET: $CRON_SECRET"

# Deve rispondere 401 — se risponde 200, il segreto non sta proteggendo nulla
curl -i -X POST "$BACKEND_URL/api/v1/cron/check-updates" \
     -H "X-CRON-SECRET: sbagliato"

# Deve rispondere 401
curl -i -X POST "$BACKEND_URL/api/v1/cron/check-updates"
```

Su PowerShell `curl` è un alias di `Invoke-WebRequest`, che non accetta questi
flag: usare `curl.exe` esplicitamente.

Il comportamento di questi tre casi è coperto dai test
(`backend/tests/test_hardening.py::test_cron_rifiuta_i_segreti_non_validi`), ma
verificarlo anche contro l'ambiente reale ha senso: è l'unico modo di accorgersi
che il segreto configurato sullo scheduler e quello sul backend sono diversi.

## Rotazione del segreto

`CRON_SECRET` non ha scadenza e non viene ruotato da solo. Per cambiarlo senza
finestre di errore: aggiornare prima il backend, poi lo scheduler entro la
finestra fra due esecuzioni. Un giro saltato non ha conseguenze — il successivo
recupera i video non ancora analizzati.
