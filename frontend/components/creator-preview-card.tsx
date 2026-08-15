"use client";

import { BadgeCheck, Loader2 } from "lucide-react";
import { useState } from "react";
import { SiInstagram, SiTiktok, SiYoutube } from "react-icons/si";

import { CreatorAvatar } from "@/components/creator-avatar";
import { Button } from "@/components/ui/button";
import { formatFollowers } from "@/lib/format";
import { PLATFORM_FOLLOWER_NOUN, PLATFORM_LABELS, type Platform } from "@/lib/types";

/**
 * Anteprima dell'autore del video che si sta per analizzare, con l'azione di
 * monitoraggio.
 *
 * È una **scorciatoia** verso la stessa entità della pagina "Creator
 * monitorati": stesso endpoint, stessa tabella, stesso limite di piano. Il
 * componente non lo sa e non deve saperlo — riceve `isFollowing` e chiama
 * `onToggleFollow`, e chi lo monta decide cosa significhino.
 */

const PLATFORM_ICON: Record<Platform, React.ReactNode> = {
  instagram: <SiInstagram className="size-4" aria-hidden />,
  youtube_shorts: <SiYoutube className="size-4" aria-hidden />,
  tiktok: <SiTiktok className="size-4" aria-hidden />,
};

export interface CreatorPreviewCardProps {
  avatarUrl: string | null;
  displayName: string;
  username: string;
  isVerified?: boolean;
  followerCount: number;
  platform: Platform;
  isFollowing: boolean;
  onToggleFollow: () => void;
  isLoading?: boolean;
}

export function CreatorPreviewCard({
  avatarUrl,
  displayName,
  username,
  isVerified,
  followerCount,
  platform,
  isFollowing,
  onToggleFollow,
  isLoading,
}: CreatorPreviewCardProps) {
  // Smettere di seguire cancella la riga del creator, e con essa
  // l'attribuzione degli insight già prodotti: `insights.creator_id` è
  // `ON DELETE SET NULL`, quindi il legame non torna nemmeno riaggiungendolo.
  // Un click di troppo su un pulsante che l'attimo prima diceva "Segui" è
  // esattamente il modo in cui si perde quel dato, quindi la direzione
  // distruttiva chiede conferma — come già fa la riga della tabella nella
  // pagina "Creator monitorati", che per la stessa azione mostra "Rimuovere?".
  const [confermaRimozione, setConfermaRimozione] = useState(false);

  function handleFollowClick() {
    if (isFollowing && !confermaRimozione) {
      setConfermaRimozione(true);
      return;
    }
    setConfermaRimozione(false);
    onToggleFollow();
  }

  return (
    <div className="border-border bg-card/60 flex items-center justify-between gap-4 rounded-xl border px-5 py-4">
      <div className="flex min-w-0 items-center gap-4">
        <CreatorAvatar url={avatarUrl} name={displayName} className="size-14" />

        {/* `items-center` sul contenitore centra questo blocco rispetto
            all'altezza dell'avatar. */}
        <div className="flex min-w-0 flex-col justify-center gap-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium">{displayName}</span>
            <span className="text-muted-foreground" aria-hidden>
              ·
            </span>
            <span className="text-muted-foreground truncate">@{username}</span>
            {isVerified && (
              <BadgeCheck
                className="size-3.5 shrink-0 text-sky-400"
                aria-label="Account verificato"
              />
            )}
          </div>
          <p className="text-muted-foreground text-sm">
            {formatFollowers(followerCount)} {PLATFORM_FOLLOWER_NOUN[platform]}
          </p>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-3">
        <span
          className="bg-muted text-muted-foreground flex size-8 items-center justify-center rounded-full"
          title={PLATFORM_LABELS[platform]}
        >
          {PLATFORM_ICON[platform]}
          <span className="sr-only">{PLATFORM_LABELS[platform]}</span>
        </span>

        {confermaRimozione && (
          <span className="text-muted-foreground hidden text-xs sm:inline">
            Non seguire più?
          </span>
        )}

        <Button
          size="sm"
          variant={isFollowing ? "outline" : "default"}
          onClick={handleFollowClick}
          onBlur={() => setConfermaRimozione(false)}
          disabled={isLoading}
        >
          {isLoading ? (
            <Loader2 className="size-4 animate-spin" aria-hidden />
          ) : confermaRimozione ? (
            "Conferma"
          ) : isFollowing ? (
            "Seguito"
          ) : (
            "Segui"
          )}
        </Button>
      </div>
    </div>
  );
}
