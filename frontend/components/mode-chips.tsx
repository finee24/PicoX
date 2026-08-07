"use client";

import type { InsightFilterMode } from "@/lib/types";
import { cn } from "@/lib/utils";

/** `undefined` = nessun filtro (Tutti). */
export type ModeFilter = InsightFilterMode | undefined;

const CHIPS: { value: ModeFilter; label: string; hint: string }[] = [
  { value: undefined, label: "Tutti", hint: "Tutti gli insight" },
  { value: "INFO", label: "INFO", hint: "Solo analisi del contenuto" },
  { value: "STYLE", label: "STYLE", hint: "Solo analisi dello stile" },
  {
    value: "SCRIPT",
    label: "Script generati",
    // Non è una modalità di analisi: filtra sulla presenza dello script.
    hint: "Insight che includono uno script inverso riutilizzabile",
  },
];

export function ModeChips({
  value,
  onChange,
}: {
  value: ModeFilter;
  onChange: (mode: ModeFilter) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2" role="group" aria-label="Filtra per tipo di analisi">
      {CHIPS.map((chip) => {
        const active = chip.value === value;
        return (
          <button
            key={chip.label}
            type="button"
            onClick={() => onChange(chip.value)}
            title={chip.hint}
            aria-pressed={active}
            className={cn(
              "rounded-full border px-3.5 py-1.5 text-sm transition-colors",
              "focus-visible:ring-ring focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-background focus-visible:outline-none",
              active
                ? "border-foreground/20 bg-foreground text-background font-medium"
                : "border-border text-muted-foreground hover:text-foreground hover:border-foreground/30",
            )}
          >
            {chip.label}
          </button>
        );
      })}
    </div>
  );
}
