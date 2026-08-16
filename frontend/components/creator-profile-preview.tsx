"use client";

import { AlertTriangle, BadgeCheck, Loader2, Lock, UserX } from "lucide-react";

import { CreatorAvatar } from "@/components/creator-avatar";
import { formatFollowers } from "@/lib/format";
import {
  PLATFORM_FOLLOWER_NOUN,
  PLATFORM_LABELS,
  type CreatorValidation,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Esito di una verifica, in forma di card.
 *
 * Vive in un componente proprio e non dentro il form dei creator perché la
 * resa di un esito è un'altra cosa dal deciderne le conseguenze: qui si sceglie
 * *come si vede* che un account è privato, altrove *se questo impedisce di
 * aggiungerlo*.
 *
 * (Una versione precedente di questo commento diceva che la card è mostrata in
 * due punti. Non lo è: la dashboard usa `creator-preview-card.tsx`, che è un
 * componente diverso con un'azione propria.)
 *
 * Non chiama nulla: riceve l'esito già ottenuto. Chi la usa decide **quando**
 * validare, ed è una decisione che costa denaro (l'endpoint dietro chiama API a
 * consumo), quindi non può stare in un componente di presentazione.
 */

export function CreatorProfilePreviewSkeleton() {
  return (
    <div
      className="border-border/60 text-muted-foreground flex items-center gap-3 rounded-lg border px-3 py-2.5 text-sm"
      role="status"
      aria-live="polite"
    >
      <Loader2 className="size-4 animate-spin" aria-hidden />
      Verifica dell&apos;account in corso…
    </div>
  );
}

export function CreatorProfilePreview({
  validation,
  className,
}: {
  validation: CreatorValidation;
  className?: string;
}) {
  const { profile, exists, is_public: isPublic, platform } = validation;

  // Account inesistente: non c'è un profilo da mostrare, e dirlo è tutto il
  // contenuto della card.
  if (!exists || !profile) {
    return (
      <div
        className={cn(
          "border-destructive/30 bg-destructive/5 flex items-center gap-3 rounded-lg border px-3 py-2.5",
          className,
        )}
        role="status"
        aria-live="polite"
      >
        <UserX className="size-5 shrink-0 text-red-400" aria-hidden />
        <div className="text-sm">
          <p className="font-medium">Account non trovato</p>
          <p className="text-muted-foreground">
            Nessun account <span className="font-mono">@{validation.normalized_identifier}</span> su{" "}
            {PLATFORM_LABELS[platform]}. Controlla l&apos;username.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn("border-border/60 space-y-2 rounded-lg border px-3 py-2.5", className)}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center gap-3">
        <CreatorAvatar url={profile.avatar_url} name={profile.display_name} />

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium">{profile.display_name}</span>
            {profile.is_verified && (
              <BadgeCheck className="size-4 shrink-0 text-sky-400" aria-label="Account verificato" />
            )}
          </div>
          <p className="text-muted-foreground truncate text-sm">
            @{profile.username} · {PLATFORM_LABELS[platform]}
          </p>
        </div>

        <div className="shrink-0 text-right">
          <p className="text-sm font-medium">{formatFollowers(profile.follower_count)}</p>
          <p className="text-muted-foreground text-xs">
            {PLATFORM_FOLLOWER_NOUN[platform]}
          </p>
        </div>
      </div>

      {/* Un profilo privato esiste ed è mostrabile, ma i suoi video non sono
          raggiungibili: monitorarlo non produrrebbe alcun insight, e nel
          frattempo il cron continuerebbe a interrogarlo a ogni giro occupando
          uno slot del piano. Da qui il blocco — il ragionamento per esteso sta
          accanto a `bloccatoDallaVerifica` in `creators-view.tsx`.

          Il testo dice cosa fare, non solo cosa non si può fare: senza il
          "riprova quando sarà pubblico" un utente resta davanti a un pulsante
          spento senza sapere se il problema è suo. */}
      {!isPublic && (
        <p className="flex items-start gap-2 text-sm text-amber-400">
          <Lock className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>
            Profilo privato: i suoi video non sono accessibili, quindi non c&apos;è nulla da
            analizzare. Non puoi monitorarlo finché resta così — riprova se diventerà pubblico.
          </span>
        </p>
      )}
    </div>
  );
}

/** Verifica non riuscita: a non rispondere è stato il provider, non l'account. */
export function CreatorValidationFailure({ message }: { message: string }) {
  return (
    <div
      className="border-border/60 flex items-start gap-3 rounded-lg border px-3 py-2.5 text-sm"
      role="status"
      aria-live="polite"
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-400" aria-hidden />
      <div>
        <p className="text-muted-foreground">{message}</p>
        {/* La verifica è un aiuto, non un cancello: se il servizio esterno è
            giù, l'utente deve poter aggiungere il creator lo stesso. */}
        <p className="text-muted-foreground">Puoi aggiungere il creator comunque.</p>
      </div>
    </div>
  );
}
