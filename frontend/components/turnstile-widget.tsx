"use client";

import Script from "next/script";
import { useEffect, useImperativeHandle, useRef, useState } from "react";

import { TURNSTILE_SITE_KEY } from "@/lib/env";

/**
 * `render=explicit` e non il rendering implicito con `class="cf-turnstile"`.
 *
 * Quello implicito cerca i contenitori una volta sola, quando lo script parte:
 * un widget montato dopo — ed è il nostro caso, il form è un client component
 * che compare a navigazione avvenuta — non verrebbe mai istanziato. Quello
 * esplicito restituisce anche l'id del widget, che serve per il `reset`.
 */
const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

interface TurnstileRenderOptions {
  sitekey: string;
  theme?: "light" | "dark" | "auto";
  callback?: (token: string) => void;
  "error-callback"?: () => void;
  "expired-callback"?: () => void;
}

interface TurnstileApi {
  render: (container: HTMLElement, options: TurnstileRenderOptions) => string;
  reset: (widgetId?: string) => void;
  remove: (widgetId?: string) => void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

export interface TurnstileHandle {
  /** Azzera il widget e il token: da chiamare dopo **ogni** tentativo. */
  reset: () => void;
}

interface TurnstileWidgetProps {
  /** Riceve il token quando la sfida è risolta, `null` quando decade. */
  onToken: (token: string | null) => void;
  ref?: React.Ref<TurnstileHandle>;
}

/**
 * Widget Cloudflare Turnstile.
 *
 * Il token che produce è **a uso singolo**: chi lo consuma deve chiamare
 * `reset()` sull'handle, altrimenti un secondo invio riuserebbe lo stesso token
 * e Supabase lo rifiuterebbe — con un errore che parla di captcha non valido
 * mentre all'utente il widget appare ancora spuntato di verde.
 *
 * `reset()` è **verificato nel browser**, non solo per lettura: dopo un invio
 * respinto da Supabase il widget si ri-renderizza invece di restare con il
 * token già speso.
 *
 * COSA SI VEDE IN SVILUPPO, con la sitekey di test `1x00000000000000000000AA`.
 * Sono due comportamenti diversi, entrambi osservati nel browser — non uno solo:
 *
 * - **Al primo render non disegna nulla.** Nessun iframe, nessuna casella:
 *   rende un contenitore vuoto e riempie subito
 *   `input[name="cf-turnstile-response"]` con il token fittizio
 *   `XXXX.DUMMY.TOKEN.XXXX`. Lo spazio è riservato ma resta vuoto — è il
 *   comportamento atteso, non un difetto di montaggio.
 * - **Dopo un `reset()` si disegna**, transitoriamente: compare il box
 *   Cloudflare con la scritta «Solo per test. Se visibile, segnalarlo al
 *   proprietario del sito», e poi torna a riempirsi da solo.
 *
 * La prima versione di questa nota diceva che con questa chiave il widget non
 * disegna **mai** nulla: era un assoluto tratto da una sola osservazione, e il
 * re-render dopo il reset lo ha smentito. Con una sitekey reale il widget si
 * disegna in entrambi i casi.
 */
export function TurnstileWidget({ onToken, ref }: TurnstileWidgetProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | null>(null);
  const [scriptPronto, setScriptPronto] = useState(false);

  // `onToken` in un ref, non nelle dipendenze dell'effetto: il chiamante passa
  // quasi sempre una closure nuova a ogni render, e usarla come dipendenza
  // distruggerebbe e ricreerebbe il widget sotto le dita dell'utente — che
  // vedrebbe la sfida azzerarsi da sola mentre digita l'email.
  const onTokenRef = useRef(onToken);
  useEffect(() => {
    onTokenRef.current = onToken;
  }, [onToken]);

  useImperativeHandle(
    ref,
    () => ({
      reset() {
        if (widgetIdRef.current !== null) {
          window.turnstile?.reset(widgetIdRef.current);
        }
        // Anche lo stato del chiamante va azzerato: il widget resettato non
        // richiama `callback` finché l'utente non risolve una nuova sfida, e
        // senza questo il token vecchio resterebbe in memoria come valido.
        onTokenRef.current(null);
      },
    }),
    [],
  );

  useEffect(() => {
    const container = containerRef.current;
    const api = window.turnstile;
    if (!scriptPronto || !container || !api || widgetIdRef.current !== null) {
      return;
    }

    widgetIdRef.current = api.render(container, {
      sitekey: TURNSTILE_SITE_KEY,
      theme: "auto",
      callback: (token) => onTokenRef.current(token),
      // Turnstile scade il token dopo alcuni minuti. Senza questo, un form
      // lasciato aperto invierebbe un token già morto.
      "expired-callback": () => onTokenRef.current(null),
      "error-callback": () => onTokenRef.current(null),
    });

    return () => {
      if (widgetIdRef.current !== null) {
        api.remove(widgetIdRef.current);
        widgetIdRef.current = null;
      }
    };
  }, [scriptPronto]);

  return (
    <>
      {/*
        `onReady` e non `onLoad`: il primo scatta anche a ogni rimontaggio del
        componente, il secondo solo al primo caricamento dello script (docs di
        Next 16 in `node_modules/next/dist/docs/01-app/03-api-reference/02-components/script.md`).
        Passando da /login a /register lo script è già in pagina, quindi con
        `onLoad` il widget non verrebbe mai istanziato la seconda volta.
      */}
      <Script src={SCRIPT_SRC} strategy="afterInteractive" onReady={() => setScriptPronto(true)} />
      <div ref={containerRef} />
    </>
  );
}
