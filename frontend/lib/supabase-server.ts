import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

import { SUPABASE_ANON_KEY, SUPABASE_URL } from "@/lib/env";

/**
 * Client Supabase per Server Component e Route Handler.
 *
 * In Next.js 16 `cookies()` è asincrona: l'accesso sincrono, tollerato in 15,
 * è stato rimosso.
 *
 * `setAll` può fallire quando viene invocata da un Server Component, dove i
 * cookie di risposta sono già stati inviati. È un caso previsto e si ignora:
 * il refresh del token viene comunque riscritto da `proxy.ts`, che gira prima
 * del rendering ed è l'unico punto in cui la scrittura è sempre possibile.
 */
export async function createSupabaseServerClient() {
  const cookieStore = await cookies();

  return createServerClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          for (const { name, value, options } of cookiesToSet) {
            cookieStore.set(name, value, options);
          }
        } catch {
          // Invocata da un Server Component: nessuna azione, ci pensa il proxy.
        }
      },
    },
  });
}
