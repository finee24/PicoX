# Deploy del frontend su Vercel

Cosa fa `vercel.json` e — soprattutto — cosa **non** può fare, perché la
maggior parte della configurazione che conta vive altrove.

## Cosa c'è in `vercel.json`

| Voce | Perché |
|---|---|
| `regions: ["fra1"]` | `proxy.ts` gira a ogni richiesta e chiama `getUser()` su Supabase. Con la funzione lontana dal progetto Supabase si paga un round-trip transatlantico **su ogni navigazione**. |
| `Content-Type` del manifest | Il Web Share Target viene registrato solo se il manifest è servito come `application/manifest+json`. Alcune configurazioni lo servono come `application/json` e la voce di condivisione non compare, senza alcun errore visibile. |
| `Cache-Control: max-age=0` sul manifest | Il manifest cambia raramente ma quando cambia (nuovo `share_target`) deve propagarsi subito: una PWA installata con un manifest vecchio in cache resta rotta finché l'utente non la reinstalla. |

Gli header di sicurezza **non** sono qui: stanno in `next.config.ts`, che li
applica a ogni risposta. Duplicarli significherebbe avere due posti da
aggiornare e nessuna garanzia che restino allineati.

## Cosa va configurato nella dashboard (non è esprimibile in `vercel.json`)

1. **Root Directory: `frontend`.** Il repository è un monorepo; senza questa
   impostazione Vercel cerca un `package.json` in radice e il build fallisce.
   È l'errore più comune al primo deploy.

2. **Variabili d'ambiente.** Solo queste tre, tutte pubbliche per costruzione —
   `NEXT_PUBLIC_*` viene sostituita a build time con una stringa letterale e
   finisce nel bundle servito al browser:

   | Variabile | Valore |
   |---|---|
   | `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` |
   | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | chiave anon (pubblica per progettazione, protetta dal RLS) |
   | `NEXT_PUBLIC_BACKEND_URL` | URL pubblico del backend su Render, **senza slash finale** |

   Nessun'altra variabile va aggiunta con il prefisso `NEXT_PUBLIC_`.
   `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`, `APIFY_API_TOKEN` e
   `CRON_SECRET` non hanno alcuna ragione di esistere in questo progetto Vercel:
   il frontend non li usa, e con quel prefisso finirebbero nel JavaScript
   scaricato da chiunque apra il sito.

3. **Supabase Auth → URL Configuration.** Aggiungere il dominio Vercel fra i
   *Redirect URLs*, altrimenti il link di conferma email rimanda a `localhost`.

## Dopo il primo deploy

Il backend accetta un solo origin. Su Render va impostata
`FRONTEND_URL=https://<progetto>.vercel.app` (senza slash finale, senza path) e
va fatto ripartire il servizio: finché quel valore non combacia con l'origin del
browser, ogni chiamata fallisce con un errore CORS che nella console appare come
un generico "failed to fetch".

Gli URL di preview (`*-git-<branch>-*.vercel.app`) hanno un origin diverso a
ogni branch e **non** sono ammessi dal CORS. È voluto: un elenco di origin
aperto ai preview è un elenco che di fatto non filtra niente. Per provare un
branch contro il backend reale conviene puntare `NEXT_PUBLIC_BACKEND_URL` a un
secondo servizio Render di staging, con il proprio `FRONTEND_URL`.

## Cron

Vercel Cron sa invocare solo path della stessa applicazione Vercel: non può
chiamare direttamente il backend su Render. Per questo lo scheduling è
documentato in `backend/app/cron_config.md`, che descrive la via consigliata
(GitHub Actions) e cosa serve per usare comunque Vercel Cron.
