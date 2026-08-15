"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiError, analyzeVideo, toUserMessage } from "@/lib/api";
import { insightKeys } from "@/lib/query-keys";
import { ANALYSIS_MODE_OPTIONS, type AnalysisMode } from "@/lib/types";


interface AnalyzeInputProps {
  initialUrl?: string;
  /**
   * Il link è arrivato a una forma stabile: blur o incolla, **mai** un tasto
   * premuto. Dietro c'è una verifica che chiama API a consumo.
   */
  onUrlSettled?: (url: string) => void;
  /** Il link è cambiato: quanto mostrato sotto la barra non lo descrive più. */
  onUrlChanged?: () => void;
  /**
   * Analisi impedita da una verifica: l'autore non esiste o è privato, quindi
   * il video non è scaricabile e l'analisi fallirebbe **dopo** aver pagato.
   */
  blocked?: boolean;
}

/**
 * Input rapido per analizzare un link.
 *
 * `initialUrl` arriva dallo share target Android (`/dashboard?shared_url=...`):
 * l'utente condivide un Reel da Instagram e se lo ritrova già incollato qui.
 *
 * La verifica dell'autore non è qui dentro: questo componente segnala *quando*
 * il link è stabile e riceve *se* l'analisi va impedita. Chi monta la card
 * decide il resto (`hooks/use-creator-preview.ts`), perché quello stato serve
 * anche a due componenti fratelli.
 */
export function AnalyzeInput({
  initialUrl,
  onUrlSettled,
  onUrlChanged,
  blocked = false,
}: AnalyzeInputProps) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState(initialUrl ?? "");
  const [mode, setMode] = useState<AnalysisMode>("BOTH");
  const [fieldError, setFieldError] = useState<string | null>(null);

  // Se l'app è già aperta e arriva una nuova condivisione, `initialUrl` cambia
  // senza che il componente venga rimontato. L'allineamento avviene *durante il
  // render* e non in un effect: React riesegue il render immediatamente, senza
  // il passaggio intermedio in cui l'input mostrerebbe ancora il valore vecchio.
  const [syncedSharedUrl, setSyncedSharedUrl] = useState(initialUrl);
  if (initialUrl !== syncedSharedUrl) {
    setSyncedSharedUrl(initialUrl);
    setUrl(initialUrl ?? "");
  }

  const mutation = useMutation({
    mutationFn: () => analyzeVideo({ video_url: url.trim(), analysis_mode: mode }),
    onSuccess: ({ fromCache }) => {
      setUrl("");
      setFieldError(null);

      // Il record è nuovo (o già esistente): in entrambi i casi la griglia va
      // rigenerata, perché un cache hit potrebbe comunque non essere nella
      // pagina attualmente caricata.
      void queryClient.invalidateQueries({ queryKey: insightKeys.all });

      // 200 e 201 sono esiti diversi: senza distinguerli, su un video già
      // analizzato l'utente vedrebbe "completata" senza che accada nulla.
      if (fromCache) {
        toast.info("Video già analizzato", {
          description: "L'insight era in archivio: nessuna nuova analisi eseguita.",
        });
      } else {
        toast.success("Analisi completata", {
          description: "Il nuovo insight è in cima alla griglia.",
        });
      }
    },
    onError: (error) => {
      // 422 riguarda ciò che l'utente ha scritto: va detto sul campo, non in un
      // toast che scompare.
      if (error instanceof ApiError && error.isValidation) {
        setFieldError(toUserMessage(error));
        return;
      }

      setFieldError(null);
      toast.error("Analisi non riuscita", {
        description: toUserMessage(error),
        action:
          error instanceof ApiError && error.isServiceUnavailable
            ? { label: "Riprova", onClick: () => mutation.mutate() }
            : undefined,
      });
    },
  });

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!url.trim() || mutation.isPending || blocked) return;
    setFieldError(null);
    mutation.mutate();
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-2">
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="flex-1">
          <label htmlFor="video-url" className="sr-only">
            Link del video da analizzare
          </label>
          <Input
            id="video-url"
            type="url"
            inputMode="url"
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              if (fieldError) setFieldError(null);
              onUrlChanged?.();
            }}
            onBlur={(e) => onUrlSettled?.(e.target.value)}
            onPaste={(e) => {
              // Al momento dell'evento il campo contiene ancora il valore
              // vecchio: si legge al giro successivo. `element` è il nodo DOM,
              // che resta valido oltre la vita dell'evento sintetico.
              const element = e.currentTarget;
              window.setTimeout(() => onUrlSettled?.(element.value), 0);
            }}
            placeholder="Incolla un link a Reel, TikTok o Short…"
            disabled={mutation.isPending}
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? "video-url-error" : undefined}
          />
        </div>

        <Select
          value={mode}
          onValueChange={(next) => setMode(next as AnalysisMode)}
          disabled={mutation.isPending}
        >
          <SelectTrigger className="sm:w-44" aria-label="Modalità di analisi">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ANALYSIS_MODE_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Button type="submit" disabled={!url.trim() || mutation.isPending || blocked}>
          {mutation.isPending ? (
            <Loader2 className="size-4 animate-spin" aria-hidden />
          ) : (
            <Sparkles className="size-4" aria-hidden />
          )}
          {mutation.isPending ? "Analisi…" : "Analizza"}
        </Button>
      </div>

      {fieldError && (
        <p id="video-url-error" role="alert" className="text-sm text-red-400">
          {fieldError}
        </p>
      )}

      {mutation.isPending && (
        <p className="text-muted-foreground text-sm" role="status">
          Scarico il video e lo analizzo: può richiedere un minuto.
        </p>
      )}
    </form>
  );
}
