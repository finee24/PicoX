"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Plus, Trash2, Users } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ApiError,
  createCreator,
  deleteCreator,
  fetchCreators,
  toUserMessage,
  updateCreator,
} from "@/lib/api";
import {
  PLATFORM_LABELS,
  type AnalysisMode,
  type Creator,
  type CreatorListResponse,
  type Platform,
} from "@/lib/types";

const PLATFORMS: Platform[] = ["instagram", "tiktok", "youtube_shorts"];

const MODES: { value: AnalysisMode; label: string }[] = [
  { value: "BOTH", label: "Completa" },
  { value: "INFO", label: "Solo contenuto" },
  { value: "STYLE", label: "Solo stile" },
];

const CREATORS_KEY = ["creators"] as const;

function AddCreatorForm() {
  const queryClient = useQueryClient();

  const [username, setUsername] = useState("");
  const [platform, setPlatform] = useState<Platform>("instagram");
  const [mode, setMode] = useState<AnalysisMode>("BOTH");
  const [fieldError, setFieldError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      createCreator({
        // La '@' iniziale la toglie anche il backend, ma inviarla pulita evita
        // che '@nome' e 'nome' sembrino due creator diversi in fase di errore.
        username: username.trim().replace(/^@/, ""),
        platform,
        analysis_mode: mode,
      }),
    onSuccess: (creator) => {
      setUsername("");
      setFieldError(null);
      void queryClient.invalidateQueries({ queryKey: CREATORS_KEY });
      toast.success("Creator aggiunto", {
        description: `@${creator.username} è ora monitorato su ${PLATFORM_LABELS[creator.platform]}.`,
      });
    },
    onError: (error) => {
      // 409 (già monitorato) e 422 (username non valido) riguardano il campo:
      // vanno mostrati accanto all'input, non in un toast che sparisce.
      if (error instanceof ApiError && (error.isConflict || error.isValidation)) {
        setFieldError(toUserMessage(error));
        return;
      }
      toast.error("Non riesco ad aggiungere il creator", {
        description: toUserMessage(error),
      });
    },
  });

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!username.trim() || mutation.isPending) return;
    setFieldError(null);
    mutation.mutate();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Aggiungi un creator</CardTitle>
        <CardDescription>
          Il job periodico controlla i nuovi video dei creator attivi e li analizza da solo.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-[1fr_auto_auto_auto]">
            <div className="space-y-1.5">
              <Label htmlFor="creator-username">Username</Label>
              <Input
                id="creator-username"
                value={username}
                onChange={(e) => {
                  setUsername(e.target.value);
                  if (fieldError) setFieldError(null);
                }}
                placeholder="@username"
                disabled={mutation.isPending}
                aria-invalid={fieldError ? true : undefined}
                aria-describedby={fieldError ? "creator-username-error" : undefined}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="creator-platform">Piattaforma</Label>
              <Select
                value={platform}
                onValueChange={(next) => setPlatform(next as Platform)}
                disabled={mutation.isPending}
              >
                <SelectTrigger id="creator-platform" className="sm:w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PLATFORMS.map((value) => (
                    <SelectItem key={value} value={value}>
                      {PLATFORM_LABELS[value]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="creator-mode">Analisi</Label>
              <Select
                value={mode}
                onValueChange={(next) => setMode(next as AnalysisMode)}
                disabled={mutation.isPending}
              >
                <SelectTrigger id="creator-mode" className="sm:w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MODES.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="flex items-end">
              <Button
                type="submit"
                disabled={!username.trim() || mutation.isPending}
                className="w-full sm:w-auto"
              >
                {mutation.isPending ? (
                  <Loader2 className="size-4 animate-spin" aria-hidden />
                ) : (
                  <Plus className="size-4" aria-hidden />
                )}
                Aggiungi
              </Button>
            </div>
          </div>

          {fieldError && (
            <p id="creator-username-error" role="alert" className="text-sm text-red-400">
              {fieldError}
            </p>
          )}
        </form>
      </CardContent>
    </Card>
  );
}

function CreatorRow({ creator }: { creator: Creator }) {
  const queryClient = useQueryClient();
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const toggle = useMutation({
    mutationFn: (isActive: boolean) => updateCreator(creator.id, { is_active: isActive }),
    // Aggiornamento ottimistico: uno switch che aspetta il round-trip prima di
    // muoversi sembra rotto.
    onMutate: async (isActive) => {
      await queryClient.cancelQueries({ queryKey: CREATORS_KEY });
      const previous = queryClient.getQueryData<CreatorListResponse>(CREATORS_KEY);

      queryClient.setQueryData<CreatorListResponse>(CREATORS_KEY, (old) =>
        old
          ? {
              ...old,
              items: old.items.map((item) =>
                item.id === creator.id ? { ...item, is_active: isActive } : item,
              ),
            }
          : old,
      );

      return { previous };
    },
    onError: (error, _isActive, context) => {
      if (context?.previous) {
        queryClient.setQueryData(CREATORS_KEY, context.previous);
      }
      toast.error("Non riesco ad aggiornare il creator", {
        description: toUserMessage(error),
      });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: CREATORS_KEY });
    },
  });

  const remove = useMutation({
    mutationFn: () => deleteCreator(creator.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: CREATORS_KEY });
      toast.success("Creator rimosso", {
        description: "Gli insight già generati restano in archivio.",
      });
    },
    onError: (error) => {
      setConfirmingDelete(false);
      toast.error("Non riesco a rimuovere il creator", {
        description: toUserMessage(error),
      });
    },
  });

  const modeLabel = MODES.find((m) => m.value === creator.analysis_mode)?.label;

  return (
    <TableRow>
      <TableCell className="font-medium">@{creator.username}</TableCell>
      <TableCell className="text-muted-foreground">
        {PLATFORM_LABELS[creator.platform]}
      </TableCell>
      <TableCell>
        <Select
          value={creator.analysis_mode}
          onValueChange={(next) =>
            updateCreator(creator.id, { analysis_mode: next as AnalysisMode })
              .then(() => queryClient.invalidateQueries({ queryKey: CREATORS_KEY }))
              .catch((error: unknown) =>
                toast.error("Non riesco ad aggiornare la modalità", {
                  description: toUserMessage(error),
                }),
              )
          }
        >
          <SelectTrigger size="sm" className="w-36" aria-label={`Modalità per @${creator.username}`}>
            <SelectValue>{modeLabel}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {MODES.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-2">
          <Switch
            checked={creator.is_active}
            onCheckedChange={(checked) => toggle.mutate(checked)}
            disabled={toggle.isPending}
            aria-label={`Monitoraggio di @${creator.username}`}
          />
          <span className="text-muted-foreground text-sm">
            {creator.is_active ? "Attivo" : "In pausa"}
          </span>
        </div>
      </TableCell>
      <TableCell className="text-right">
        {confirmingDelete ? (
          <div className="flex items-center justify-end gap-2">
            <span className="text-muted-foreground text-xs">Rimuovere?</span>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              {remove.isPending && <Loader2 className="size-3 animate-spin" aria-hidden />}
              Sì
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirmingDelete(false)}>
              No
            </Button>
          </div>
        ) : (
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setConfirmingDelete(true)}
            className="text-muted-foreground hover:text-red-400"
            aria-label={`Rimuovi @${creator.username}`}
          >
            <Trash2 className="size-4" aria-hidden />
          </Button>
        )}
      </TableCell>
    </TableRow>
  );
}

export function CreatorsView() {
  const creatorsQuery = useQuery({
    queryKey: CREATORS_KEY,
    queryFn: ({ signal }) => fetchCreators(signal),
  });

  const creators = creatorsQuery.data?.items ?? [];

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 px-4 py-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Creator monitorati</h1>
        <p className="text-muted-foreground text-sm">
          Aggiungi gli account da tenere d&apos;occhio: i nuovi video vengono analizzati
          automaticamente.
        </p>
      </div>

      <AddCreatorForm />

      {creatorsQuery.isPending ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, index) => (
            <Skeleton key={index} className="h-14 w-full" />
          ))}
        </div>
      ) : creatorsQuery.isError ? (
        <div className="border-destructive/30 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border px-6 py-12 text-center">
          <AlertTriangle className="size-6 text-red-400" aria-hidden />
          <p className="text-muted-foreground max-w-md text-sm">
            {toUserMessage(creatorsQuery.error)}
          </p>
          <Button variant="outline" size="sm" onClick={() => void creatorsQuery.refetch()}>
            Riprova
          </Button>
        </div>
      ) : creators.length === 0 ? (
        <div className="border-border/60 flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-16 text-center">
          <div className="bg-muted flex size-12 items-center justify-center rounded-full">
            <Users className="text-muted-foreground size-6" aria-hidden />
          </div>
          <div className="space-y-1">
            <h2 className="font-medium">Nessun creator monitorato</h2>
            <p className="text-muted-foreground max-w-md text-sm">
              Aggiungine uno qui sopra, oppure continua ad analizzare singoli link dalla
              dashboard.
            </p>
          </div>
        </div>
      ) : (
        <div className="border-border overflow-hidden rounded-xl border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Username</TableHead>
                <TableHead>Piattaforma</TableHead>
                <TableHead>Analisi</TableHead>
                <TableHead>Monitoraggio</TableHead>
                <TableHead className="w-32 text-right">Azioni</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {creators.map((creator) => (
                <CreatorRow key={creator.id} creator={creator} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
