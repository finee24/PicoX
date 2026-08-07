"use client";

import { createBrowserClient } from "@supabase/ssr";

import { SUPABASE_ANON_KEY, SUPABASE_URL } from "@/lib/env";

/**
 * Client Supabase per il browser.
 *
 * `createBrowserClient` di `@supabase/ssr` conserva la sessione nei **cookie**
 * e non in `localStorage`: è ciò che permette a `proxy.ts` di leggerla lato
 * server e decidere il redirect prima ancora di renderizzare la pagina.
 *
 * L'istanza è un singleton per tab. Crearne una nuova a ogni render
 * registrerebbe più listener di `onAuthStateChange` sullo stesso storage.
 */
let client: ReturnType<typeof createBrowserClient> | undefined;

export function getSupabaseBrowserClient() {
  if (!client) {
    client = createBrowserClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  }
  return client;
}
