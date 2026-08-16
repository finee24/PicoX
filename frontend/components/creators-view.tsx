"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Plus, Trash2, Users } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import {
  CreatorProfilePreview,
  CreatorProfilePreviewSkeleton,
  CreatorValidationFailure,
} from "@/components/creator-profile-preview";
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
  validateCreator,
} from "@/lib/api";
import { creatorKeys, insightKeys } from "@/lib/query-keys";
import {
  ANALYSIS_MODE_OPTIONS,
  PLATFORM_LABELS,
  type AnalysisMode,
  type Creator,
  type CreatorListResponse,
  type CreatorValidation,
  type Platform,
} from "@/lib/types";

const PLATFORMS: Platform[] = ["instagram", "tiktok", "youtube_shorts"];



function AddCreatorForm() {
  const queryClient = useQueryClient();

  const [username, setUsername] = useState("");
  const [platform, setPlatform] = useState<Platform>("instagram");
  const [mode, setMode] = useState<AnalysisMode>("BOTH");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [validation, setValidation] = useState<CreatorValidation | null>(null);
  const [validationFailure, setValidationFailure] = useState<string | null>(null);

  // Ultimo valore per cui è partita una verifica. Serve a scartare le risposte
  // superate dai fatti: due blur ravvicinati possono tornare in ordine inverso,
  // e senza questo controllo la card mostrerebbe il profilo di ciò che c'era
  // scritto prima.
  //
  // Lo stesso guardiano esiste in `hooks/use-creator-preview.ts`, che verifica
  // lo stesso endpoint. **La somiglianza è superficiale e la separazione è
  // deliberata**: i due divergono su cosa fanno dell'esito, ed è una differenza
  // di prodotto, non di implementazione.
  //
  //   * qui l'esito grezzo va alla card, che sa mostrare da sé "privato" e "non
  //     trovato"; là diventa una stringa d'avviso e la card sparisce del tutto,
  //     insieme al pulsante che ci sta dentro;
  //   * qui un 422 diventa un errore di campo; là viene ingoiato, perché su un
  //     link di un Reel l'autore non è ricavabile e non saperlo non è un motivo
  //     per impedire un'analisi che funziona.
  //
  // **Su chi si può seguire i due ora concordano**, e prima no: qui il profilo
  // privato non bloccava. Non è stata un'unificazione, sono arrivati allo stesso
  // punto per strade diverse — là perché un video privato non è scaricabile,
  // qui perché un creator privato in watchlist fa lavorare il cron a vuoto per
  // sempre. Restano due implementazioni perché restano diverse le altre due
  // righe qui sopra, non per inerzia.
  //
  // Se un domani servisse davvero fonderle, la cosa da estrarre è il solo
  // guardiano "vince l'ultima richiesta", non la macchina a stati.
  const richiestaCorrente = useRef("");

  const verificaAccount = useMutation({
    mutationFn: (value: string) => validateCreator({ input: value, platform }),
    onSuccess: (esito, value) => {
      if (richiestaCorrente.current !== value) return;
      setValidation(esito);
      setValidationFailure(null);
      setFieldError(null);
      // Il link vince sul selettore: chi incolla un URL Instagram avendo
      // "TikTok" selezionato intende Instagram, e il backend ha già validato
      // quella piattaforma. Allineare il menu evita di creare il creator sulla
      // piattaforma sbagliata subito dopo una verifica riuscita.
      setPlatform(esito.platform);
      // Il campo mostra l'handle estratto: chi ha incollato un URL vede cosa
      // verrà effettivamente aggiunto. Il valore verificato diventa anche
      // l'ultimo richiesto, altrimenti il blur successivo — su un campo che
      // ora contiene l'handle e non più l'URL — rifarebbe la stessa verifica.
      setUsername(esito.normalized_identifier);
      richiestaCorrente.current = esito.normalized_identifier;
    },
    onError: (error, value) => {
      if (richiestaCorrente.current !== value) return;
      // Si dimentica il valore richiesto, così un secondo blur ritenta invece
      // di essere scartato come "già verificato": una verifica fallita non ha
      // verificato nulla.
      richiestaCorrente.current = "";
      setValidation(null);
      // Un 422 riguarda ciò che è stato scritto — un link di un video, un host
      // non supportato — e va accanto al campo, come già fanno 409 e 422 sulla
      // creazione. Tutto il resto (quota esaurita, provider giù) riguarda il
      // servizio e non il campo: si mostra come avviso, senza impedire nulla.
      if (error instanceof ApiError && error.isValidation) {
        setFieldError(toUserMessage(error));
        setValidationFailure(null);
        return;
      }
      setValidationFailure(toUserMessage(error));
    },
  });

  /**
   * Avvia la verifica per il valore indicato.
   *
   * Chiamata **solo** su gesti conclusi — blur e incolla — mai su `onChange`:
   * dietro c'è un endpoint che paga Apify o consuma quota YouTube, quindi una
   * verifica per tasto premuto sarebbe una chiamata pagata per lettera scritta.
   */
  function avviaVerifica(valore: string) {
    const pulito = valore.trim();
    // Già verificato: rifarlo costerebbe comunque una riga di quota, perché il
    // tetto è per utente e non sa nulla della cache del browser.
    if (!pulito || pulito === richiestaCorrente.current) return;
    richiestaCorrente.current = pulito;
    verificaAccount.mutate(pulito);
  }

  const mutation = useMutation({
    mutationFn: () =>
      createCreator({
        // Dopo una verifica riuscita si usa l'handle normalizzato dal backend:
        // è la stessa stringa con cui l'account è stato trovato, quindi non può
        // divergere da ciò che l'utente ha appena visto nella card.
        // La '@' iniziale la toglie anche il backend, ma inviarla pulita evita
        // che '@nome' e 'nome' sembrino due creator diversi in fase di errore.
        username: validation?.normalized_identifier ?? username.trim().replace(/^@/, ""),
        platform,
        analysis_mode: mode,
      }),
    onSuccess: (creator) => {
      setUsername("");
      setFieldError(null);
      // La card si riferiva al creator appena aggiunto: lasciarla sotto a un
      // campo ormai vuoto la farebbe sembrare l'anteprima del prossimo.
      setValidation(null);
      setValidationFailure(null);
      richiestaCorrente.current = "";
      void queryClient.invalidateQueries({ queryKey: creatorKeys.all });
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

  // Si blocca quando il provider ha detto qualcosa di definitivo sull'account:
  // non esiste, oppure esiste ma è privato. In entrambi i casi aggiungerlo
  // produce un creator che non genererà mai un insight.
  //
  // Sul profilo privato la regola è cambiata, e vale la pena dire perché. Prima
  // si avvisava soltanto, con l'argomento che un account può tornare pubblico e
  // che la decisione è di chi lo aggiunge. L'argomento regge, ma nessuno aveva
  // seguito cosa succede dopo: `creators` non ha una colonna che ricordi lo
  // stato privato, quindi il cron non può saperlo. Enumera i creator attivi e
  // chiama Apify per ciascuno **a ogni giro, per sempre**, su un profilo da cui
  // non tornerà mai un video. In più lo slot resta occupato: 30 attivi sul
  // piano gratuito.
  //
  // Un avviso che l'utente può ignorare gli fa scegliere fra due cose di cui
  // vede solo una — nella schermata non c'è nulla che dica «e continuerà a
  // costare». Finché quello stato non è persistito e il cron non lo salta,
  // «avvisa» e «lascia fare» non sono la stessa cosa.
  //
  // Una verifica **non riuscita** invece non blocca: lì non sappiamo nulla
  // dell'account, e non sapere non è un motivo per impedire.
  const bloccatoDallaVerifica =
    validation !== null && (!validation.exists || !validation.is_public);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!username.trim() || mutation.isPending || bloccatoDallaVerifica) return;
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
                  // L'esito mostrato si riferisce a ciò che c'era scritto
                  // prima: appena il testo cambia non descrive più nulla.
                  setValidation(null);
                  setValidationFailure(null);
                }}
                onBlur={(e) => avviaVerifica(e.target.value)}
                onPaste={(e) => {
                  // Al momento dell'evento il campo contiene ancora il valore
                  // vecchio: si legge al giro successivo, così un incolla su un
                  // campo già pieno viene verificato per intero e non per la
                  // sola parte incollata. `element` è il nodo DOM, che resta
                  // valido oltre la vita dell'evento sintetico.
                  const element = e.currentTarget;
                  window.setTimeout(() => avviaVerifica(element.value), 0);
                }}
                placeholder="@username oppure link del profilo"
                disabled={mutation.isPending}
                aria-invalid={fieldError ? true : undefined}
                aria-describedby={fieldError ? "creator-username-error" : undefined}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="creator-platform">Piattaforma</Label>
              <Select
                value={platform}
                onValueChange={(next) => {
                  setPlatform(next as Platform);
                  // L'esito riguardava l'altra piattaforma: tenerlo visibile
                  // farebbe aggiungere un creator su TikTok mostrando la card
                  // del profilo Instagram. Si azzera anche l'ultimo valore
                  // richiesto, così il blur successivo rifà la verifica —
                  // stesso handle, piattaforma diversa, altra domanda.
                  setValidation(null);
                  setValidationFailure(null);
                  richiestaCorrente.current = "";
                }}
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
                  {ANALYSIS_MODE_OPTIONS.map((option) => (
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
                disabled={
                  !username.trim() ||
                  mutation.isPending ||
                  // Un click sul pulsante fa prima perdere il fuoco al campo,
                  // quindi la verifica parte e questo lo disabilita per il
                  // tempo della chiamata: l'utente clicca una seconda volta
                  // avendo però visto l'esito, che è il punto della verifica.
                  verificaAccount.isPending ||
                  bloccatoDallaVerifica
                }
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

          {verificaAccount.isPending ? (
            <CreatorProfilePreviewSkeleton />
          ) : validation ? (
            <CreatorProfilePreview validation={validation} />
          ) : validationFailure ? (
            <CreatorValidationFailure message={validationFailure} />
          ) : null}
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
      await queryClient.cancelQueries({ queryKey: creatorKeys.all });
      const previous = queryClient.getQueryData<CreatorListResponse>(creatorKeys.all);

      queryClient.setQueryData<CreatorListResponse>(creatorKeys.all, (old) =>
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
        queryClient.setQueryData(creatorKeys.all, context.previous);
      }
      toast.error("Non riesco ad aggiornare il creator", {
        description: toUserMessage(error),
      });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: creatorKeys.all });
    },
  });

  const remove = useMutation({
    mutationFn: () => deleteCreator(creator.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: creatorKeys.all });
      // Anche gli insight: `insights.creator_id` è `ON DELETE SET NULL`, quindi
      // la cancellazione di un creator cambia righe che stanno nell'altra
      // cache. Senza questa riga gli insight già scaricati conservavano il
      // vecchio `creator_id` fino al refetch — incoerenza invisibile finché
      // qualcosa non prova a risolvere quell'id.
      // La chiave è il prefisso `["insights"]`: le query reali sono
      // `["insights", { search, mode }]`, e invalidare il prefisso le copre
      // tutte, come già fa `analyze-input.tsx`.
      void queryClient.invalidateQueries({ queryKey: insightKeys.all });
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

  // Stessa API di `toggle` qui sopra, e ora anche stesso meccanismo. Era
  // scritta come catena `.then().catch()`: l'unica mutazione del frontend a non
  // passare da `useMutation`, e quindi l'unica a **non** attraversare la
  // `MutationCache` di `app/providers.tsx`, dove un 401 innesca il logout
  // centralizzato. Su questa singola azione una sessione scaduta mostrava un
  // toast e lasciava l'utente in una pagina che non poteva più funzionare.
  //
  // Niente aggiornamento ottimistico, a differenza di `toggle`: qui non ce
  // n'era, e aggiungerlo avrebbe cambiato ciò che si vede.
  const changeMode = useMutation({
    mutationFn: (mode: AnalysisMode) => updateCreator(creator.id, { analysis_mode: mode }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: creatorKeys.all });
    },
    onError: (error) => {
      toast.error("Non riesco ad aggiornare la modalità", {
        description: toUserMessage(error),
      });
    },
  });

  const modeLabel = ANALYSIS_MODE_OPTIONS.find((m) => m.value === creator.analysis_mode)?.label;

  return (
    <TableRow>
      <TableCell className="font-medium">@{creator.username}</TableCell>
      <TableCell className="text-muted-foreground">
        {PLATFORM_LABELS[creator.platform]}
      </TableCell>
      <TableCell>
        <Select
          value={creator.analysis_mode}
          onValueChange={(next) => changeMode.mutate(next as AnalysisMode)}
        >
          <SelectTrigger size="sm" className="w-36" aria-label={`Modalità per @${creator.username}`}>
            <SelectValue>{modeLabel}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {ANALYSIS_MODE_OPTIONS.map((option) => (
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
    queryKey: creatorKeys.all,
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
