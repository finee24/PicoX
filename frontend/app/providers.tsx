"use client";

import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError } from "@/lib/api";
import { getSupabaseBrowserClient } from "@/lib/supabase-client";

export function Providers({ children }: { children: React.ReactNode }) {
  const router = useRouter();

  /**
   * `QueryClient` creato dentro `useState` e non a livello di modulo: un'istanza
   * di modulo sarebbe condivisa fra richieste diverse sullo stesso server, e i
   * dati di un utente finirebbero nella cache di un altro.
   */
  const [queryClient] = useState(() => {
    /**
     * Punto unico di gestione della sessione scaduta.
     *
     * `lib/api.ts` si limita a sollevare `ApiError(401)`: la reazione — logout,
     * svuotamento della cache, redirect — vive qui, dove esistono il router e
     * il `QueryClient`. Il flag evita che dieci query fallite in parallelo
     * facciano dieci logout e dieci redirect.
     */
    let handlingExpiredSession = false;

    async function onExpiredSession() {
      if (handlingExpiredSession) return;
      handlingExpiredSession = true;

      await getSupabaseBrowserClient().auth.signOut();
      // La cache contiene dati dell'utente che sta uscendo.
      client.clear();
      router.replace("/login?expired=1");
    }

    function handleError(error: unknown) {
      if (error instanceof ApiError && error.isUnauthorized) {
        void onExpiredSession();
      }
    }

    const client = new QueryClient({
      queryCache: new QueryCache({ onError: handleError }),
      mutationCache: new MutationCache({ onError: handleError }),
      defaultOptions: {
        queries: {
          staleTime: 30_000,
          refetchOnWindowFocus: false,
          retry(failureCount, error) {
            // 401 e 422 non migliorano ritentando: il primo richiede un nuovo
            // login, il secondo un input diverso. Sul 503, che è transitorio,
            // si concede un tentativo in più.
            if (error instanceof ApiError) {
              if (error.status === 401 || error.status === 422) return false;
              if (error.status === 503) return failureCount < 2;
              if (error.status >= 400 && error.status < 500) return false;
            }
            return failureCount < 1;
          },
        },
        mutations: {
          retry: false,
        },
      },
    });

    return client;
  });

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
