import type { NextConfig } from "next";

/**
 * Header di sicurezza applicati a ogni risposta.
 *
 * Difesa in profondità: non sostituiscono la validazione lato applicazione, ma
 * chiudono classi di attacco che non dipendono da bug nel nostro codice.
 */
const securityHeaders = [
  // Le pagine autenticate non vanno incorniciate: senza questo, un sito terzo
  // può sovrapporre un iframe invisibile di /creators e far cliccare all'utente
  // gli switch di monitoraggio o il pulsante di rimozione.
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },

  // Impedisce al browser di indovinare un content-type diverso da quello
  // dichiarato, che è il modo in cui un file caricato diventa uno script.
  { key: "X-Content-Type-Options", value: "nosniff" },

  // Gli URL di Picox contengono termini di ricerca e id: non devono finire
  // interi nel Referer di un dominio esterno.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },

  // Nessuna funzionalità del prodotto usa questi permessi.
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
  },
];

const nextConfig: NextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
