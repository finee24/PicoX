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

/** Secondi → "1:23". */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}
