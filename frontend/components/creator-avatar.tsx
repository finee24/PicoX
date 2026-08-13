"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

/**
 * Immagine di profilo di un creator, con ripiego sull'iniziale.
 *
 * Vive in un modulo proprio perché la usano due card diverse — l'anteprima
 * nella pagina Creator e quella sopra il feed degli insight — e la parte
 * delicata non è il cerchio: è il ripiego quando l'URL non carica, la
 * `referrerPolicy` e la scelta di non passare da `next/image`. Duplicarla
 * significherebbe correggerla in un posto solo la prima volta che serve.
 *
 * Un `<img>` e non `next/image`: l'avatar arriva da CDN di terze parti con
 * hostname che cambiano per shard (`scontent-xxx.cdninstagram.com`,
 * `p16-sign-va.tiktokcdn.com`), quindi non sono elencabili in
 * `images.remotePatterns`. Passarli comunque dal loader di Next significherebbe
 * far scaricare al nostro server immagini di host arbitrari, cioè un SSRF con
 * l'ottimizzazione delle immagini come vettore.
 */
export function CreatorAvatar({
  url,
  name,
  className,
}: {
  url: string | null;
  name: string;
  className?: string;
}) {
  const [rotta, setRotta] = useState(false);
  const iniziale = name.trim().charAt(0).toUpperCase() || "?";

  if (!url || rotta) {
    return (
      <div
        className={cn(
          "bg-muted text-muted-foreground flex size-11 shrink-0 items-center justify-center rounded-full text-sm font-medium",
          className,
        )}
        aria-hidden
      >
        {iniziale}
      </div>
    );
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt=""
      loading="lazy"
      // L'URL di Picox contiene l'handle che si sta verificando: non deve finire
      // nei log della CDN di una piattaforma terza.
      referrerPolicy="no-referrer"
      onError={() => setRotta(true)}
      className={cn("bg-muted size-11 shrink-0 rounded-full object-cover", className)}
    />
  );
}
