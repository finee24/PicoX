/**
 * Normalizza una destinazione di redirect proveniente dall'esterno.
 *
 * `?redirect=` e `?next=` arrivano dalla query string, quindi da chiunque sappia
 * costruire un link. Se il valore finisse in `router.push()` o in
 * `NextResponse.redirect()` senza controlli, `/login?redirect=//evil.com`
 * porterebbe l'utente su un dominio altrui **subito dopo un login riuscito**:
 * il momento in cui è più disposto a fidarsi di quello che vede.
 *
 * Il controllo non è su prefissi di stringa. `"/".startsWith` lascia passare
 * `//evil.com` (URL protocol-relative) e `/\evil.com` (i browser normalizzano
 * il backslash in slash), e la lista dei trucchi non finisce lì: tabulazioni e
 * newline incorporate vengono rimosse dal parser prima della risoluzione.
 *
 * Si delega quindi al parser URL, che è lo stesso che deciderà davvero dove
 * andare: si risolve il valore contro un'origin sentinella e si accetta solo se
 * l'origin non è cambiata. Qualunque forma che il parser risolve altrove viene
 * scartata; qualunque forma che non risolve altrove non è, per definizione, una
 * navigazione cross-origin.
 */

const SENTINEL_ORIGIN = "https://picox.invalid";

export const DEFAULT_REDIRECT = "/dashboard";

export function safeInternalPath(
  value: string | null | undefined,
  fallback: string = DEFAULT_REDIRECT,
): string {
  if (typeof value !== "string" || value.length === 0) return fallback;

  try {
    const resolved = new URL(value, SENTINEL_ORIGIN);
    if (resolved.origin !== SENTINEL_ORIGIN) return fallback;
    return `${resolved.pathname}${resolved.search}${resolved.hash}`;
  } catch {
    return fallback;
  }
}

/**
 * URL esterno da mettere in un `href`, oppure `null`.
 *
 * `insight.video_url` è validato dal backend prima di essere scritto, quindi
 * oggi è già sicuro. Ma è l'unico valore del database che finisce in un `href`,
 * e React non blocca `javascript:` negli attributi href — in React 19 si limita
 * a un warning. Il controllo dello schema costa una riga e chiude il sink.
 */
export function httpUrlOrNull(value: string | null | undefined): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}
