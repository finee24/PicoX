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
 * **Solleva su un valore non numerico, e lo fa apposta.** È la differenza con
 * `formatFollowers` e `formatDuration` qui accanto, che davanti a un input
 * illeggibile restituiscono `"—"`: là il trattino è un'*affermazione vera* — il
 * backend manda `0` sia per "nessun follower" sia per "il profilo li nasconde",
 * e mostrare zero sarebbe più forte del dato. Qui no. Qui un campo assente non
 * significa niente sul video: significa che **il record non ha la forma che
 * crediamo**, e un trattino lo maschererebbe da misura mancante.
 *
 * I campi di `style_data` arrivano da una colonna `jsonb` che contiene lo schema
 * **in vigore al momento dell'analisi**. Un record scritto prima che un campo
 * esistesse non ce l'ha, e quel giorno serve accorgersene: un dato mancante deve
 * essere visibile come anomalia, non nascosto dietro un default.
 *
 * Il messaggio nomina il campo e il tipo ricevuto, perché l'alternativa —
 * lasciar fallire `.toFixed()` — dà `Cannot read properties of undefined`, che
 * dice cosa è successo ma non dove.
 *
 * **Il raggio d'azione è l'intera rotta.** Non esiste alcun error boundary nel
 * frontend, quindi questa eccezione non abbatte la singola card ma la pagina che
 * la contiene. È noto e accettato: la scelta è fra un guasto rumoroso e un
 * archivio che mostra numeri in cui non si può avere fiducia. Chi volesse
 * restringerlo aggiunga `app/dashboard/error.tsx`, non un fallback qui.
 *
 * Il separatore decimale resta il punto, a differenza di `formatFollowers` che
 * usa quello italiano (`Intl.NumberFormat("it")` → `"12,3 Mln"`). L'incoerenza è
 * precedente e va decisa a parte: correggerla qui cambierebbe ciò che l'utente
 * legge.
 */
export function formatSeconds(seconds: unknown, decimali = 0, campo = "valore"): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    throw new TypeError(
      `style_data.${campo}: atteso un numero non negativo, ricevuto ${
        seconds === null ? "null" : typeof seconds
      }. Il record è stato scritto con uno schema precedente.`,
    );
  }
  return `${seconds.toFixed(decimali)}s`;
}

/** Secondi → "1:23". */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}
