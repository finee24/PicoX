"use client";

import { ExternalLink, Quote, Scissors } from "lucide-react";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { formatDuration, insightTitle, relativeTime, shortenUrl } from "@/lib/format";
import { httpUrlOrNull } from "@/lib/safe-redirect";
import {
  BEAT_SECTION_LABELS,
  HOOK_TYPE_LABELS,
  PACING_LABELS,
  type Creator,
  type Insight,
} from "@/lib/types";

/** Il backend registra la modalità eseguita; `BOTH` mostra entrambi i badge. */
function ModeBadges({ mode }: { mode: Insight["analysis_mode"] }) {
  const showInfo = mode === "INFO" || mode === "BOTH";
  const showStyle = mode === "STYLE" || mode === "BOTH";

  return (
    <div className="flex gap-1.5">
      {showInfo && (
        <Badge className="border-emerald-500/30 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/15">
          INFO
        </Badge>
      )}
      {showStyle && (
        <Badge className="border-violet-500/30 bg-violet-500/15 text-violet-300 hover:bg-violet-500/15">
          STYLE
        </Badge>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="space-y-0.5">
      <p className="text-muted-foreground text-xs tracking-wide uppercase">{label}</p>
      <p className="text-sm">{value}</p>
    </div>
  );
}

export function InsightCard({
  insight,
  creator,
  onKeywordClick,
}: {
  insight: Insight;
  /** Risolto dal chiamante: `/insights` restituisce solo `creator_id`. */
  creator?: Creator;
  onKeywordClick: (keyword: string) => void;
}) {
  const { summary_data: info, style_data: style, inverse_script_template: script } = insight;
  const keyPoints = info?.key_points ?? [];
  // Unico valore del database che finisce in un `href`: si accetta solo http/https.
  const videoHref = httpUrlOrNull(insight.video_url);

  return (
    <Card className="flex h-full flex-col gap-0 overflow-hidden py-0">
      <CardHeader className="gap-3 p-4 pb-3">
        <div className="flex items-start justify-between gap-3">
          <ModeBadges mode={insight.analysis_mode} />
          <span className="text-muted-foreground shrink-0 text-xs">
            {relativeTime(insight.created_at)}
          </span>
        </div>

        <div className="space-y-1">
          <h3 className="text-base leading-snug font-medium">{insightTitle(insight)}</h3>
          <p className="text-muted-foreground text-xs">
            {creator ? (
              <span className="text-foreground/70">@{creator.username}</span>
            ) : (
              "Link singolo"
            )}
            {" · "}
            {videoHref ? (
              <a
                href={videoHref}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 underline-offset-4 hover:underline"
              >
                {shortenUrl(insight.video_url)}
                <ExternalLink className="size-3" aria-hidden />
              </a>
            ) : (
              <span>{shortenUrl(insight.video_url)}</span>
            )}
          </p>
        </div>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col gap-3 p-4 pt-0">
        {info?.summary && (
          <p className="text-muted-foreground line-clamp-3 text-sm">{info.summary}</p>
        )}

        {keyPoints.length > 0 && (
          <ul className="space-y-1.5 text-sm">
            {keyPoints.slice(0, 4).map((point, index) => (
              <li key={index} className="flex gap-2">
                <span aria-hidden className="mt-2 size-1 shrink-0 rounded-full bg-emerald-400" />
                <span className="leading-snug">{point}</span>
              </li>
            ))}
          </ul>
        )}

        {(style || script) && (
          <Accordion type="single" collapsible className="w-full">
            {style && (
              <AccordionItem value="hook" className="border-border/60">
                <AccordionTrigger className="py-2.5 text-sm hover:no-underline">
                  <span className="flex items-center gap-2">
                    <Quote className="size-3.5 text-violet-400" aria-hidden />
                    Analisi dell&apos;hook e dello stile
                  </span>
                </AccordionTrigger>
                <AccordionContent className="space-y-3 pb-3">
                  <blockquote className="border-l-2 border-violet-500/40 pl-3 text-sm italic">
                    “{style.hook}”
                  </blockquote>
                  <div className="grid grid-cols-2 gap-3">
                    <Field
                      label="Tipo di hook"
                      value={HOOK_TYPE_LABELS[style.hook_type] ?? style.hook_type}
                    />
                    <Field label="Durata hook" value={`${Math.round(style.hook_duration_seconds)}s`} />
                    <Field label="Ritmo" value={PACING_LABELS[style.pacing] ?? style.pacing} />
                    <Field
                      label="Inquadratura media"
                      value={`${style.average_shot_duration_seconds.toFixed(1)}s`}
                    />
                  </div>
                  <Field label="Tono" value={style.tone_of_voice} />
                  <Field label="Stile visivo" value={style.visual_style} />
                  <Field label="Audio" value={style.audio_style} />
                  <Field label="Testo a schermo" value={style.text_overlay_usage} />
                  {style.editing_techniques?.length > 0 && (
                    <Field label="Montaggio" value={style.editing_techniques.join(" · ")} />
                  )}
                  {style.retention_devices?.length > 0 && (
                    <Field label="Ritenzione" value={style.retention_devices.join(" · ")} />
                  )}
                  <Field label="Call to action" value={style.cta} />
                </AccordionContent>
              </AccordionItem>
            )}

            {script && (
              <AccordionItem value="script" className="border-border/60 border-b-0">
                <AccordionTrigger className="py-2.5 text-sm hover:no-underline">
                  <span className="flex items-center gap-2">
                    <Scissors className="size-3.5 text-emerald-400" aria-hidden />
                    Script inverso
                  </span>
                </AccordionTrigger>
                <AccordionContent className="space-y-3 pb-3">
                  <div className="flex items-baseline justify-between gap-2">
                    <p className="text-sm font-medium">{script.title}</p>
                    <span className="text-muted-foreground shrink-0 text-xs">
                      {formatDuration(script.total_duration_seconds)}
                    </span>
                  </div>

                  <Field label="Hook riutilizzabile" value={script.reusable_hook_template} />
                  <Field label="CTA riutilizzabile" value={script.reusable_cta_template} />

                  {script.beats?.length > 0 && (
                    <div className="space-y-2">
                      <p className="text-muted-foreground text-xs tracking-wide uppercase">
                        Struttura
                      </p>
                      <ol className="space-y-2">
                        {script.beats.map((beat, index) => (
                          <li key={index} className="border-border border-l pl-3">
                            <p className="text-muted-foreground font-mono text-xs">
                              {beat.timestamp} ·{" "}
                              {BEAT_SECTION_LABELS[beat.section] ?? beat.section}
                            </p>
                            <p className="text-sm">{beat.content}</p>
                            <p className="text-muted-foreground text-xs italic">{beat.purpose}</p>
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}

                  <Field label="Come riadattarlo" value={script.adaptation_notes} />
                </AccordionContent>
              </AccordionItem>
            )}
          </Accordion>
        )}

        {insight.keywords.length > 0 && (
          <>
            <Separator className="mt-auto" />
            <div className="flex flex-wrap gap-1.5">
              {insight.keywords.map((keyword) => (
                <button
                  key={keyword}
                  type="button"
                  onClick={() => onKeywordClick(keyword)}
                  className="border-border text-muted-foreground hover:border-foreground/30 hover:text-foreground focus-visible:ring-ring focus-visible:ring-offset-background rounded-full border px-2.5 py-0.5 text-xs transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none"
                  title={`Cerca "${keyword}"`}
                >
                  {keyword}
                </button>
              ))}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
