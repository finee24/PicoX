import type { Insight } from "@/lib/types";

/** URL ridotto a `dominio/ultimo-segmento`, per quando non c'è di meglio da mostrare. */
export function shortenUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.replace(/^www\./, "");
    const segments = parsed.pathname.split("/").filter(Boolean);
    const tail = segments.at(-1);
    return tail ? `${host}/${tail}` : host;
  } catch {
    return url;
  }
}

/**
 * Titolo della card.
 *
 * Il backend non espone un campo "titolo": si usa `main_topic`, che è pensato
 * per stare in poche parole. Un insight in sola modalità STYLE ha
 * `summary_data: null`, da cui i fallback.
 */
export function insightTitle(insight: Insight): string {
  return (
    insight.summary_data?.main_topic?.trim() ||
    insight.inverse_script_template?.title?.trim() ||
    shortenUrl(insight.video_url)
  );
}

const RELATIVE_FORMATTER = new Intl.RelativeTimeFormat("it", { numeric: "auto" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 60 * 60 * 1000],
  ["month", 30 * 24 * 60 * 60 * 1000],
  ["day", 24 * 60 * 60 * 1000],
  ["hour", 60 * 60 * 1000],
  ["minute", 60 * 1000],
];

/** "3 giorni fa", "un'ora fa". */
export function relativeTime(isoDate: string): string {
  const timestamp = new Date(isoDate).getTime();
  if (Number.isNaN(timestamp)) return "";

  const elapsed = timestamp - Date.now();
  const magnitude = Math.abs(elapsed);

  for (const [unit, ms] of UNITS) {
    if (magnitude >= ms) {
      return RELATIVE_FORMATTER.format(Math.round(elapsed / ms), unit);
    }
  }
  return "adesso";
}

const COMPACT_FORMATTER = new Intl.NumberFormat("it", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/**
 * Follower/iscritti in forma compatta: 12345 → "12.345" → "12,3 Mln".
 *
 * Il backend restituisce `0` sia per "nessun follower" sia per "il profilo li
 * nasconde", perché non ha modo di distinguerli: qui si mostra un trattino
 * invece di uno zero, che sarebbe un'affermazione più forte del dato.
 */
export function formatFollowers(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return "—";
  return COMPACT_FORMATTER.format(count);
}

/**
 * Una misura in secondi con la sua unità: `3.2` → `"3.2s"`.
 *
 * Esiste per lo stesso motivo di `formatFollowers` qui sopra — la guardia su
 * `Number.isFinite` — ma per un caso diverso. I campi di `style_data` arrivano
 * da una colonna `jsonb` che contiene lo schema **in vigore al momento
 * dell'analisi**: un record scritto prima che un campo esistesse non ce l'ha, e
 * `undefined.toFixed()` non è un valore sbagliato, è un'eccezione che porta giù
 * l'intera scheda dello stile.
 *
 * Oggi il caso non si presenta — tutti i record hanno i campi — ed è esattamente
 * il motivo per cui era facile scriverlo senza guardia. `lib/types.ts` lo dice
 * già nel commento in testa: quei payload vanno letti in modo difensivo.
 *
 * **Il separatore decimale resta il punto**, a differenza di `formatFollowers`
 * che usa quello italiano (`Intl.NumberFormat("it")` → `"12,3 Mln"`).
 * L'incoerenza è precedente a questa funzione ed è stata conservata di
 * proposito: correggerla qui avrebbe cambiato ciò che l'utente legge dentro un
 * intervento che non doveva cambiare nulla. Va decisa a parte.
 */
export function formatSeconds(seconds: number | undefined, decimali = 0): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  return `${seconds.toFixed(decimali)}s`;
}

/** Secondi → "1:23". */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}
