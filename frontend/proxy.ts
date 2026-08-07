import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { SUPABASE_ANON_KEY, SUPABASE_URL } from "@/lib/env";

/**
 * Guardia di autenticazione, eseguita prima del rendering di ogni pagina.
 *
 * **Nota sul nome del file.** In Next.js 16 la convenzione `middleware.ts` è
 * deprecata e rinominata in `proxy.ts`, con la funzione esportata che si chiama
 * `proxy` (vedi `node_modules/next/dist/docs/01-app/02-guides/upgrading/version-16.md`).
 * Il comportamento è identico a quello del vecchio middleware.
 *
 * Si usa `getUser()` e non `getSession()`: `getSession()` si limita a leggere
 * il cookie, che è dato non attendibile lato server perché il client può
 * scriverlo. `getUser()` valida il token contro Supabase, ed è l'unica verifica
 * che ha senso in un controllo d'accesso.
 */

/** Rotte raggiungibili senza sessione. */
const PUBLIC_PATHS = ["/login", "/register", "/auth/callback"];

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));
}

export async function proxy(request: NextRequest) {
  // La risposta viene ricreata a ogni scrittura di cookie: è il modo in cui
  // `@supabase/ssr` propaga un token rinnovato sia alla richiesta in corso sia
  // al browser.
  let response = NextResponse.next({ request });

  const supabase = createServerClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value } of cookiesToSet) {
          request.cookies.set(name, value);
        }
        response = NextResponse.next({ request });
        for (const { name, value, options } of cookiesToSet) {
          response.cookies.set(name, value, options);
        }
      },
    },
  });

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname, search } = request.nextUrl;

  if (!user && !isPublicPath(pathname)) {
    const loginUrl = new URL("/login", request.url);
    // Si conserva la destinazione per riportarci l'utente dopo l'accesso.
    if (pathname !== "/") {
      loginUrl.searchParams.set("redirect", `${pathname}${search}`);
    }
    return NextResponse.redirect(loginUrl);
  }

  if (user && (pathname === "/login" || pathname === "/register")) {
    return NextResponse.redirect(new URL("/dashboard", request.url));
  }

  return response;
}

export const config = {
  matcher: [
    /*
     * Tutto tranne gli asset statici. Senza questa esclusione il proxy girerebbe
     * anche su CSS, JS e immagini, e il redirect a /login impedirebbe il
     * caricamento della pagina di login stessa.
     *
     * `manifest.json` è escluso di proposito: il browser lo richiede senza
     * cookie quando installa la PWA, e un redirect lo renderebbe illeggibile.
     */
    "/((?!_next/static|_next/image|favicon.ico|manifest\\.json|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
