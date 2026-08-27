"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, RotateCcw, Sparkles, X } from "lucide-react";
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
import {
  ANALYSIS_MODE_OPTIONS,
  type AnalysisMode,
  type AnalyzeVideoPayload,
} from "@/lib/types";


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
 * Il tentativo che non è andato a buon fine, con il payload **esatto** che era
 * stato spedito.
 *
 * Non è una copia decorativa dello stato del form. Rileggere `url` e `mode` al
 * momento del clic su "Riprova" ritenterebbe *ciò che c'è scritto adesso*, che
 * dopo un incolla o un cambio di modalità è un'altra analisi — pagata come
 * tale. Il payload va congelato quando fallisce, non ricostruito dopo.
 */
interface TentativoFallito {
  payload: AnalyzeVideoPayload;
  message: string;
}

function etichettaModalita(mode: AnalysisMode): string {
  return ANALYSIS_MODE_OPTIONS.find((option) => option.value === mode)?.label ?? mode;
}

/**
 * Ciò che resta di un'analisi fallita, dopo che il toast è sparito.
 *
 * Il toast dura pochi secondi e con lui spariva l'unico modo di ritentare: un
 * `503` di capacità dura invece minuti (`context.md`, «Il 503 di Gemini è
 * intermittente»), quindi quasi sempre più a lungo dell'avviso che lo annuncia.
 * Questo riquadro resta finché l'analisi non riesce o non lo si chiude.
 */
function CardTentativoFallito({
  tentativo,
  isPending,
  onRetry,
  onDismiss,
}: {
  tentativo: TentativoFallito;
  isPending: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      className="border-destructive/30 bg-destructive/5 flex flex-col gap-3 rounded-xl border px-4 py-3 sm:flex-row sm:items-start"
      role="status"
      aria-live="polite"
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-red-400" aria-hidden />

      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-medium">Analisi non riuscita</p>
        <p className="text-muted-foreground text-sm">{tentativo.message}</p>
        {/* `break-all`: un link lungo non deve allargare la griglia qui sotto. */}
        <p className="text-muted-foreground/80 break-all text-xs">
          {tentativo.payload.video_url} ·{" "}
          {etichettaModalita(tentativo.payload.analysis_mode)}
        </p>
      </div>

      <div className="flex shrink-0 items-center gap-1">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onRetry}
          disabled={isPending}
        >
          {isPending ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <RotateCcw className="size-3.5" aria-hidden />
          )}
          Riprova
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onDismiss}
          disabled={isPending}
          aria-label="Ignora il tentativo non riuscito"
        >
          <X className="size-4" aria-hidden />
        </Button>
      </div>
    </div>
  );
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
  const [tentativoFallito, setTentativoFallito] = useState<TentativoFallito | null>(
    null,
  );

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
    // Il payload è un *argomento*, non una lettura di `url`/`mode` dallo stato:
    // è ciò che permette a "Riprova" di rispedire il tentativo congelato anche
    // quando la barra nel frattempo contiene altro.
    mutationFn: (payload: AnalyzeVideoPayload) => analyzeVideo(payload),
    onSuccess: ({ fromCache }) => {
      setUrl("");
      setFieldError(null);
      // Il video è passato: il riquadro non ha più niente da ritentare. Vale
      // anche quando a riuscire è stata la barra su un altro link — l'analisi
      // congelata resterebbe offerta senza che nessuno l'abbia più chiesta.
      setTentativoFallito(null);

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
    onError: (error, payload) => {
      // 422 riguarda ciò che l'utente ha scritto: va detto sul campo, non in un
      // toast che scompare. Non tocca un riquadro già presente: quello riguarda
      // un altro video, ed è ancora ritentabile.
      if (error instanceof ApiError && error.isValidation) {
        setFieldError(toUserMessage(error));
        return;
      }

      setFieldError(null);
      const message = toUserMessage(error);

      // Un 401 porta a logout e redirect (`app/providers.tsx`): un riquadro da
      // ritentare resterebbe appeso a una pagina che sta per sparire.
      if (!(error instanceof ApiError && error.isUnauthorized)) {
        setTentativoFallito({ payload, message });
      }

      // Nessuna azione "Riprova" qui dentro: la teneva il toast, che dura pochi
      // secondi, e rileggeva `url` e `mode` dallo stato invece del payload
      // spedito. Ora è il riquadro qui sotto a offrirla, e sul dato giusto.
      toast.error("Analisi non riuscita", { description: message });
    },
  });

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const videoUrl = url.trim();
    if (!videoUrl || mutation.isPending || blocked) return;
    setFieldError(null);
    mutation.mutate({ video_url: videoUrl, analysis_mode: mode });
  }

  return (
    <div className="space-y-2">
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

      {/* Fuori dal `form`: sono pulsanti che non lo inviano, e dentro un form
          un `<button>` senza `type` è un submit. */}
      {tentativoFallito && (
        <CardTentativoFallito
          tentativo={tentativoFallito}
          isPending={mutation.isPending}
          // `blocked` non entra qui: riguarda l'autore del link attualmente
          // nella barra, non quello del tentativo congelato. Legarli
          // impedirebbe di ritentare un video valido perché *un altro* link,
          // incollato dopo, è di un profilo privato.
          onRetry={() => mutation.mutate(tentativoFallito.payload)}
          onDismiss={() => setTentativoFallito(null)}
        />
      )}
    </div>
  );
}
