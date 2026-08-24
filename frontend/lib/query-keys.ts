import type { ModeFilter } from "@/components/mode-chips";

/**
 * Le chiavi della cache di TanStack Query, in un posto solo.
 *
 * Non è organizzazione per il gusto di organizzare: una chiave sbagliata non
 * fallisce, **non fa niente**. `invalidateQueries` con una chiave che non
 * corrisponde a nulla ritorna senza errori, il type checker è contento, e il
 * sintomo è una lista che non si aggiorna dopo un'azione riuscita — il tipo di
 * difetto che si attribuisce al backend per ore.
 *
 * Prima stavano metà qui e metà là: `CREATORS_KEY` era una costante in
 * `creators-view`, ma non esportata, quindi `dashboard-view` riscriveva
 * `["creators"]` a mano. Due file, due fonti di verità per la stessa entry di
 * cache.
 *
 * ## `all` è un prefisso, e deve restarlo
 *
 * `insightKeys.list(...)` produce `["insights", {…}]`, e `insightKeys.all` è
 * `["insights"]`: TanStack fa corrispondenza **per prefisso**, quindi
 * invalidare `all` copre tutte le liste filtrate in una volta. È ciò che serve
 * dopo un'analisi o dopo aver rimosso un creator, quando non si sa quali filtri
 * l'utente abbia attivi. Se un domani `all` smettesse di essere un prefisso di
 * `list`, quelle invalidazioni diventerebbero silenziosamente inefficaci.
 */

export const creatorKeys = {
  /** Tutti i creator dell'utente. Il backend non pagina questa lista. */
  all: ["creators"] as const,
};

export const insightKeys = {
  /** Prefisso di ogni lista di insight, con qualunque filtro. */
  all: ["insights"] as const,

  /**
   * Una lista filtrata.
   *
   * I filtri fanno parte della chiave: cambiandoli si ottiene una lista nuova
   * invece di accodare risultati eterogenei a quelli già caricati.
   */
  list: (filtri: { search: string; mode: ModeFilter }) => ["insights", filtri] as const,
};
