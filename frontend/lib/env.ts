/**
 * Lettura delle variabili d'ambiente pubbliche.
 *
 * Next.js sostituisce `process.env.NEXT_PUBLIC_*` a build time con una
 * stringa letterale, quindi va referenziato per intero: `process.env[nome]`
 * con un indice dinamico non viene sostituito e a runtime risulta `undefined`.
 *
 * Queste tre sono le **uniche** variabili ammesse nel frontend. Ogni altra
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
