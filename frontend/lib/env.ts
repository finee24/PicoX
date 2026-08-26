/**
 * Lettura delle variabili d'ambiente pubbliche.
 *
 * Next.js sostituisce `process.env.NEXT_PUBLIC_*` a build time con una
 * stringa letterale, quindi va referenziato per intero: `process.env[nome]`
 * con un indice dinamico non viene sostituito e a runtime risulta `undefined`.
 *
 * Queste quattro sono le **uniche** variabili ammesse nel frontend. Ogni altra
 * `NEXT_PUBLIC_*` finirebbe comunque nel bundle servito al browser.
 */

function required(value: string | undefined, name: string): string {
  if (!value) {
    throw new Error(
      `Variabile d'ambiente mancante: ${name}. ` +
        "Copiare .env.local.example in .env.local e compilarla.",
    );
  }
  return value;
}

/**
 * Le sitekey di **prova** pubblicate da Cloudflare, verbatim dalla sua
 * documentazione (Turnstile → Troubleshooting → Testing).
 *
 * In un bundle di produzione ognuna di queste e' un guasto silenzioso, in una
 * delle due direzioni — e sono opposte, il che rende la diagnosi peggiore:
 *
 * - con la secret di prova configurata su Supabase, il captcha **passa sempre**
 *   e non protegge nulla, mentre tutto sembra funzionare;
 * - con la secret **reale**, il token fittizio `XXXX.DUMMY.TOKEN.XXXX` viene
 *   rifiutato e **nessuno riesce piu' ad accedere** — e' la documentazione di
 *   Cloudflare stessa a dirlo: «Production secret keys will reject the dummy
 *   token».
 *
 * Il secondo caso non e' teorico: e' successo, in una forma diversa, con il
 * captcha che bloccava il login (vedi `bug.md`, «La sessione cade dopo il
 * login»). Da quando il captcha protegge anche l'accesso e non piu' il solo
 * signup, questa chiave e' il freno del login: sbagliarla chiude fuori i
 * clienti.
 */
const SITEKEY_DI_PROVA = new Set([
  "1x00000000000000000000AA", // passa sempre, visibile
  "2x00000000000000000000AB", // fallisce sempre, visibile
  "1x00000000000000000000BB", // passa sempre, invisibile
  "2x00000000000000000000BB", // fallisce sempre, invisibile
  "3x00000000000000000000FF", // forza la sfida interattiva, visibile
]);

/**
 * La stessa famiglia riconosciuta **per forma**: una cifra, `x`, venti zeri,
 * due lettere maiuscole.
 *
 * L'elenco qui sopra e' una fotografia della documentazione di oggi, e una
 * guardia che non riconosce una variante aggiunta domani e' una guardia che non
 * guarda — cioe' esattamente il modo in cui nasce il difetto che questa riga
 * esiste per impedire. Nessuna sitekey reale ha questa forma: quelle vere
 * iniziano per `0x4` e non hanno venti zeri.
 */
const FORMA_SITEKEY_DI_PROVA = /^\dx0{20}[A-Z]{2}$/;

/**
 * Blocca il build se una sitekey di prova finisce in un bundle di produzione.
 *
 * Stesso schema di `required`: o restituisce il valore, o solleva e ferma
 * `next build`. Fuori da un build di produzione non fa nulla, perche' in
 * sviluppo la sitekey di prova e' esattamente quella che serve.
 *
 * **Conseguenza voluta, da sapere prima di incontrarla**: `npm run build` in
 * locale imposta da se' `NODE_ENV=production`, quindi con la sitekey di prova
 * in `.env.local` fallisce. E' il comportamento giusto — un bundle di
 * produzione e' un bundle di produzione da qualunque macchina esca — e per un
 * build di verifica basta sovrascrivere la variabile, come fa la CI.
 */
function vietaSitekeyDiProvaInProduzione(value: string): string {
  if (process.env.NODE_ENV !== "production") {
    return value;
  }
  if (!SITEKEY_DI_PROVA.has(value) && !FORMA_SITEKEY_DI_PROVA.test(value)) {
    return value;
  }
  throw new Error(
    `NEXT_PUBLIC_TURNSTILE_SITE_KEY e' una sitekey di PROVA di Cloudflare ` +
      `(${value}), e questo e' un build di produzione. In produzione non ` +
      "protegge nulla se la secret su Supabase e' quella di prova, e impedisce " +
      "ogni accesso se la secret e' quella reale. Usa la sitekey del progetto " +
      "Cloudflare vero, oppure sovrascrivi la variabile se questo build serve " +
      "solo a verificare la compilazione.",
  );
}

export const SUPABASE_URL = required(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  "NEXT_PUBLIC_SUPABASE_URL",
);

export const SUPABASE_ANON_KEY = required(
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
  "NEXT_PUBLIC_SUPABASE_ANON_KEY",
);

/** Senza slash finale: gli URL vengono composti come `${BACKEND_URL}/api/v1/...`. */
export const BACKEND_URL = required(
  process.env.NEXT_PUBLIC_BACKEND_URL,
  "NEXT_PUBLIC_BACKEND_URL",
).replace(/\/+$/, "");

/**
 * Sitekey del widget Turnstile sul form di registrazione.
 *
 * È pubblica per progetto: sta nell'HTML di chiunque apra la pagina, e non c'è
 * nulla da proteggere nel tenerla nel bundle. La metà che conta — la secret con
 * cui si verifica il token — non vive in questo repo: il signup non passa dal
 * nostro backend, e la verifica la fa Supabase Auth, configurata dalla sua
 * dashboard.
 *
 * `required` come le altre tre, e non opzionale con degrado silenzioso. Se
 * mancasse, il widget sparirebbe dal form e ogni registrazione fallirebbe
 * contro Supabase — che il captcha lo pretende comunque — con un errore che non
 * dice dove cercare. Meglio fallire subito e per il motivo giusto.
 */
export const TURNSTILE_SITE_KEY = vietaSitekeyDiProvaInProduzione(
  required(process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY, "NEXT_PUBLIC_TURNSTILE_SITE_KEY"),
);
