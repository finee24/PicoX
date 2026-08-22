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
export const TURNSTILE_SITE_KEY = required(
  process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY,
  "NEXT_PUBLIC_TURNSTILE_SITE_KEY",
);
