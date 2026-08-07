import { NextResponse, type NextRequest } from "next/server";

import { safeInternalPath } from "@/lib/safe-redirect";
import { createSupabaseServerClient } from "@/lib/supabase-server";

/**
 * Destinazione del link di conferma email (e di un eventuale OAuth futuro).
 *
 * Supabase rimanda qui con un `code` monouso, che va scambiato per una
 * sessione. Lo scambio deve avvenire lato server perché è qui che i cookie di
 * sessione vengono scritti.
 */
export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;

  const code = searchParams.get("code");
  // `next` arriva dalla query string: senza normalizzazione questa rotta
  // diventerebbe un open redirect (vedi `lib/safe-redirect.ts`).
  const destination = safeInternalPath(searchParams.get("next"));

  if (!code) {
    return NextResponse.redirect(new URL("/login?error=missing_code", origin));
  }

  const supabase = await createSupabaseServerClient();
  const { error } = await supabase.auth.exchangeCodeForSession(code);

  if (error) {
    // Il dettaglio resta nei log del server: al browser basta sapere che il
    // link non è più utilizzabile.
    console.error("Scambio del code di sessione non riuscito:", error.message);
    return NextResponse.redirect(new URL("/login?error=invalid_code", origin));
  }

  return NextResponse.redirect(new URL(destination, origin));
}
