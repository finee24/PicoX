"use client";

import { useMutation } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { validateCreator } from "@/lib/api";
import { PLATFORM_LABELS, type CreatorValidation } from "@/lib/types";

/**
 * Verifica dell'autore del link incollato nella barra di analisi.
 *
 * Sta in un hook e non dentro `AnalyzeInput` perché lo stato ha **tre**
 * consumatori in tre punti diversi dell'albero: la barra (che disabilita
 * "Analizza"), la card montata sotto di essa, e la lista dei creator (per
 * sapere se l'autore è già seguito). Tenerlo nella barra costringerebbe a
 * risalire con dei callback ciò che qui si legge e basta.
 *
 * ## Cosa succede quando l'autore non è ricavabile
 *
 * L'input della barra contiene il link di un **video**, non di un profilo, e da
 * un link di un video l'autore si ricava solo a volte:
 *
 * - `tiktok.com/@tizio/video/123` → sì, l'handle è nel path;
 * - `instagram.com/reel/Cxyz` e `youtube.com/shorts/abc` → **no**: lo
 *   shortcode non dice nulla su chi ha pubblicato.
 *
 * Nel secondo caso il backend risponde `422` e qui non succede niente: nessuna
 * card, nessun avviso, nessun blocco. È l'unica risposta onesta — non sapere
 * chi sia l'autore non è un motivo per impedire l'analisi, che di suo funziona
 * benissimo su quei link.
 *
 * Ricavare l'autore anche da lì si potrebbe: basterebbe far risolvere il post
 * ad Apify. Sarebbe però una chiamata a pagamento a ogni incolla, per un dato
 * che la pipeline di analisi recupera comunque poco dopo — cioè lo stesso
 * costo pagato due volte per anticipare una card.
 *
 * ## Perché `AddCreatorForm` non usa questo hook
 *
 * `components/creators-view.tsx` verifica lo stesso endpoint con lo stesso
 * guardiano anti-sorpasso, e a colpo d'occhio sembra la stessa cosa scritta due
 * volte. Non lo è: le due divergono su **cosa fanno dell'esito**, e la
 * differenza è di prodotto.
 *
 * Qui un profilo privato fa sparire la card, e con essa il pulsante che
 * contiene, perché il video non sarebbe scaricabile e l'analisi fallirebbe dopo
 * aver pagato. Là il blocco arriva per un'altra ragione — un creator privato in
 * watchlist fa lavorare il cron a vuoto a ogni giro — ma l'esito per l'utente
 * ora è lo stesso: **un profilo privato non si può seguire da nessuna delle due
 * schermate**. Non è così da sempre: fino al 16 agosto 2026 il form dei creator
 * si limitava ad avvisare.
 *
 * Restano diverse le altre due cose: qui un `422` viene ingoiato, là diventa un
 * errore accanto al campo; e qui l'esito diventa una stringa d'avviso, là va
 * grezzo a una card che sa renderlo da sé.
 *
 * Fonderle richiederebbe di scegliere una delle due semantiche, cioè di
 * cambiare il comportamento di una delle due pagine. La sola parte davvero
 * comune è il guardiano "vince l'ultima richiesta", una decina di righe: se un
 * domani ci fosse un terzo consumatore, è quello da estrarre — non la macchina
 * a stati.
 */
export interface CreatorPreviewState {
  /** Presente solo per un autore che **esiste ed è pubblico**: la card mostra quello. */
  validation: CreatorValidation | null;
  /** Motivo per cui non si può analizzare. Prende il posto della card. */
  warning: string | null;
  isPending: boolean;
  /** `true` quando l'analisi va impedita: account inesistente o privato. */
  blocked: boolean;
  /** Da chiamare su gesti conclusi — blur, incolla — mai a ogni tasto. */
  verify: (input: string) => void;
  reset: () => void;
}

export function useCreatorPreview(): CreatorPreviewState {
  const [validation, setValidation] = useState<CreatorValidation | null>(null);
  const [warning, setWarning] = useState<string | null>(null);

  // Ultimo valore verificato: due blur ravvicinati possono tornare in ordine
  // inverso, e senza questo controllo la card mostrerebbe l'autore del link
  // precedente accanto a un campo che ne contiene un altro.
  const richiestaCorrente = useRef("");

  const mutation = useMutation({
    mutationFn: (input: string) => validateCreator({ input }),
    onSuccess: (esito, input) => {
      if (richiestaCorrente.current !== input) return;

      if (esito.exists && esito.is_public && esito.profile) {
        setValidation(esito);
        setWarning(null);
        return;
      }

      setValidation(null);
      setWarning(
        esito.exists
          ? `Il profilo @${esito.normalized_identifier} è privato: i suoi video non sono ` +
              "accessibili, quindi l'analisi non può scaricarli."
          : `Non trovo l'account @${esito.normalized_identifier} su ${PLATFORM_LABELS[esito.platform]}: ` +
              "il link potrebbe puntare a un contenuto rimosso.",
      );
    },
    onError: (_error, input) => {
      if (richiestaCorrente.current !== input) return;
      // Si dimentica il valore, così un secondo blur ritenta: una verifica
      // fallita non ha verificato nulla.
      richiestaCorrente.current = "";
      setValidation(null);
      // Nessun avviso e nessun blocco, qualunque sia l'errore — ma per due
      // ragioni diverse:
      //   * 422 → l'autore non è ricavabile da questo link (vedi docstring).
      //     Non sappiamo nulla di lui, e non sapere non è un motivo per
      //     impedire un'analisi che funzionerebbe;
      //   * tutto il resto (provider giù, quota di verifiche esaurita) →
      //     riguarda una funzione accessoria di questa pagina. Un avviso
      //     accanto al campo farebbe credere che l'analisi sia a rischio,
      //     quando non è toccata; l'errore vero, se ci sarà, lo mostrerà lei.
      setWarning(null);
    },
  });

  const { mutate } = mutation;

  const verify = useCallback(
    (input: string) => {
      const pulito = input.trim();
      if (!pulito || pulito === richiestaCorrente.current) return;
      richiestaCorrente.current = pulito;
      mutate(pulito);
    },
    [mutate],
  );

  const reset = useCallback(() => {
    richiestaCorrente.current = "";
    setValidation(null);
    setWarning(null);
  }, []);

  return {
    validation,
    warning,
    isPending: mutation.isPending,
    blocked: warning !== null,
    verify,
    reset,
  };
}
