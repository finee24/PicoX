# frontend — dashboard Picox

Next.js 16 (App Router, TypeScript), Tailwind v4, Shadcn UI, TanStack Query,
Supabase Auth. Dark mode di default.

---

## Avvio locale

```bash
cd frontend
npm install
cp .env.local.example .env.local     # e compilare
npm run dev
```

`http://localhost:3000`.

**La porta 3000 non è negoziabile in sviluppo.** Il backend ammette in CORS solo
l'origin indicato dalla sua variabile `FRONTEND_URL`, che di default è
`http://localhost:3000`: su un'altra porta il browser blocca ogni chiamata.

Serve anche il backend attivo su `http://localhost:8001` — la porta che
`NEXT_PUBLIC_BACKEND_URL` si aspetta, e che lo script fissa da sé:

```bash
cd ../backend && ./dev.ps1      # bash/zsh: ./dev.sh
```

## Variabili d'ambiente

Solo tre, tutte pubbliche:

| Variabile | Uso |
| --- | --- |
| `NEXT_PUBLIC_SUPABASE_URL` | Progetto Supabase |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Chiave pubblica, soggetta a RLS |
| `NEXT_PUBLIC_BACKEND_URL` | Base URL dell'API FastAPI |

Next.js inlinea ogni `NEXT_PUBLIC_*` nel bundle servito al browser. Nessuna
chiave segreta — `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`,
`APIFY_API_TOKEN`, `CRON_SECRET` — deve comparire qui: vivono solo nel backend.
Il controllo si fa sul bundle prodotto, non sul sorgente:

```bash
npm run build
grep -r "SERVICE_ROLE\|GEMINI\|APIFY\|CRON_SECRET" .next/static/   # atteso: nessun risultato
```

---

## Struttura

```
app/
  layout.tsx              dark mode, Providers, Toaster
  providers.tsx           QueryClient + gestione unica della sessione scaduta
  page.tsx                redirect → /dashboard
  login/ register/        autenticazione
  auth/callback/route.ts  scambio del code di conferma email
  dashboard/page.tsx      legge ?shared_url= dallo share target
  creators/page.tsx
components/
  dashboard-view.tsx      ricerca, chip, griglia, infinite scroll, anteprima autore
  insight-card.tsx        badge, key_points, accordion, keyword cliccabili
  creators-view.tsx       tabella + form + toggle + rimozione
  creator-preview-card.tsx    autore del link + Segui/Seguito (sopra il feed)
  creator-profile-preview.tsx esito della verifica nel form di aggiunta
  creator-avatar.tsx      avatar con ripiego sull'iniziale, condiviso dalle due card
  analyze-input.tsx  search-bar.tsx  mode-chips.tsx
  auth-form.tsx  auth-shell.tsx  app-nav.tsx  picox-mark.tsx
  ui/                     Shadcn
lib/
  api.ts                  fetch autenticata + envelope d'errore
  types.ts                tipi allineati agli schemi Pydantic del backend
  supabase-client.ts      client browser (sessione nei cookie)
  supabase-server.ts      client server (cookies() asincrona)
  env.ts  format.ts  utils.ts
hooks/
  use-debounced-value.ts
  use-creator-preview.ts  verifica dell'autore del link incollato
proxy.ts                  guardia di autenticazione
public/manifest.json      PWA + share target
```

### `proxy.ts` e non `middleware.ts`

In **Next.js 16 la convenzione `middleware.ts` è deprecata** e rinominata in
`proxy.ts`, con la funzione esportata che si chiama `proxy`. Il comportamento è
identico. Riferimento:
`node_modules/next/dist/docs/01-app/02-guides/upgrading/version-16.md`.

Il proxy usa `supabase.auth.getUser()` e non `getSession()`: `getSession()`
legge soltanto il cookie, che lato server è un dato che il client può scrivere.
`getUser()` valida il token contro Supabase, ed è l'unica verifica che ha senso
in un controllo d'accesso.

### Altri effetti di Next.js 16

`cookies()`, `headers()`, `params` e `searchParams` sono **asincroni**: l'accesso
sincrono, tollerato in 15, è stato rimosso. Da qui le pagine `async` che fanno
`await props.searchParams`.

I tipi `PageProps<'/rotta'>` e `LayoutProps<'/'>` sono generati da
`npx next typegen` (eseguito in automatico da `next dev` e `next build`). Dopo
un checkout pulito, un `tsc --noEmit` isolato fallisce finché non li si genera.

---

## Come parla col backend

`lib/api.ts` è l'unico punto che chiama l'API. Ogni richiesta porta
`Authorization: Bearer <access_token>`, riletto dalla sessione a ogni chiamata:
il token si rinnova, e una copia in cache diventerebbe scaduta senza preavviso.

Il backend risponde sempre con lo stesso envelope
(`.claude/skills/api-conventions/SKILL.md`):

```json
{ "error": { "code": "video_too_large", "message": "…", "details": … } }
```

`ApiError` lo espone intatto, così la UI decide sul `code` e non sul testo:

| Status | Trattamento in UI |
| --- | --- |
| `422` | Messaggio **inline sul campo** — riguarda ciò che l'utente ha scritto |
| `409` | Inline sul form creator: "già nella tua watchlist" |
| `503` | Toast con azione **Riprova** — è transitorio |
| `401` | Logout, cache svuotata, redirect a `/login?expired=1` |

Il `401` è gestito in **un solo punto**, `app/providers.tsx`, tramite gli hook
`onError` di `QueryCache` e `MutationCache`. `lib/api.ts` si limita a sollevare:
un modulo di rete che naviga da solo crea un secondo sistema di navigazione che
non conosce il router.

### `200` contro `201` su `/analyze-video`

Il backend risponde **201** quando l'analisi è stata eseguita e **200** quando
il video era già in cache. `analyzeVideo()` distingue i due casi e la UI mostra
messaggi diversi: senza, su un video già analizzato l'utente leggerebbe "analisi
completata" senza che sia successo nulla.

### Lo username del creator

`GET /insights` restituisce `creator_id`, non l'handle. La dashboard tiene in
cache `GET /creators` (con `staleTime` lungo, cambiano di rado) e risolve
l'associazione lato client. Gli insight da link singolo hanno `creator_id: null`
e mostrano "Link singolo".

### Nomi dei campi

I payload seguono gli schemi Pydantic del backend: i punti chiave sono
`summary_data.key_points` e l'hook è `style_data.hook` (con `hook_type` e
`hook_duration_seconds`). Le card non hanno un campo "titolo" dedicato: si usa
`summary_data.main_topic`, con fallback a `inverse_script_template.title` e
infine all'URL abbreviato, perché un insight in sola modalità `STYLE` ha
`summary_data: null`.

---

## Condividi → Picox

`public/manifest.json` dichiara un [Web Share Target](https://developer.mozilla.org/en-US/docs/Web/Manifest/share_target):

```json
"share_target": {
  "action": "/dashboard",
  "method": "GET",
  "params": { "url": "shared_url", "text": "shared_text", "title": "shared_title" }
}
```

Su **Android**, con l'app installata come PWA da Chrome o Edge, Picox compare nel
menu di condivisione di Instagram e TikTok: il link arriva su
`/dashboard?shared_url=…` e si trova già nell'input di analisi.

Alcune app mettono l'URL in `text` invece che in `url`, quindi la dashboard
guarda anche lì ed estrae il primo link che trova.

> **iOS / Safari: non supportato.** Safari non implementa il Web Share Target,
> quindi Picox non compare nel foglio di condivisione di iOS nemmeno aggiungendo
> l'app alla schermata Home. Su iPhone il flusso resta copia del link e incolla
> nell'input della dashboard. È una limitazione della piattaforma, non
> aggirabile lato applicazione.

Per provarlo senza dispositivo Android, basta aprire a mano:
`http://localhost:3000/dashboard?shared_url=https://www.instagram.com/reel/XYZ/`

---

## Verifiche

```bash
npx tsc --noEmit    # 0 errori
npm run lint        # 0 problemi
npm run build       # build di produzione
```

Poi, con backend e frontend attivi: registrazione, login, redirect del proxy su
rotta protetta senza sessione, analisi di un link, ricerca con debounce, chip di
filtro, keyword cliccabile, CRUD creator.
