"use client";

import { useEffect, useState } from "react";

/**
 * Ritarda la propagazione di un valore finché non smette di cambiare.
 *
 * Serve alla barra di ricerca: senza, ogni tasto premuto farebbe partire una
 * query verso il backend.
 */
export function useDebouncedValue<T>(value: T, delayMs = 400): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
