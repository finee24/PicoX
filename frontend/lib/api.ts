"use client";

import { getSupabaseBrowserClient } from "@/lib/supabase-client";
import { BACKEND_URL } from "@/lib/env";
import type {
  AnalyzeVideoPayload,
  ApiErrorBody,
  Creator,
  CreatorCreatePayload,
  CreatorListResponse,
  CreatorUpdatePayload,
  CreatorValidation,
  CreatorValidationPayload,
  Insight,
  InsightListResponse,
  InsightQuery,
  ValidationDetail,
} from "@/lib/types";

/**
 * Client HTTP verso il backend Picox.
 *
 * Il backend restituisce sempre un envelope d'errore uniforme
 * (`.claude/skills/api-conventions/SKILL.md`):
 *
 *     { "error": { "code": "...", "message": "...", "details"?: ... } }
 *
 * `ApiError` lo espone intatto, così la UI può decidere in base al `code` senza
 * fare pattern matching sul testo del messaggio.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** 422 — input rifiutato: URL non valido, video troppo grande o troppo lungo. */
  get isValidation() {
    return this.status === 422;
  }

  /** 503 — Gemini, Apify o il database non disponibili. Transitorio: ha senso ritentare. */
  get isServiceUnavailable() {
    return this.status === 503;
  }

  /** 409 — es. creator già monitorato per questo utente. */
  get isConflict() {
    return this.status === 409;
  }

  get isUnauthorized() {
    return this.status === 401;
  }

  /** Dettagli di validazione, quando il backend li fornisce. */
  get validationDetails(): ValidationDetail[] {
    if (!this.isValidation || !Array.isArray(this.details)) return [];
    return this.details.filter(
      (d): d is ValidationDetail =>
        typeof d === "object" && d !== null && "field" in d && "message" in d,
    );
  }
}

/** Errore di rete: il backend non ha risposto affatto. */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkError";
  }
}

async function getAccessToken(): Promise<string | null> {
  // Il token si rilegge a ogni chiamata invece di essere memorizzato: la
  // sessione si rinnova periodicamente e una copia in cache diventerebbe
  // scaduta senza preavviso. `getSession()` restituisce quella corrente,
  // rinnovandola se necessario.
  const supabase = getSupabaseBrowserClient();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/**
 * Sessione non più valida.
 *
 * Questo modulo **non naviga**: solleva e basta. Il logout e il redirect sono
 * gestiti in un unico punto (`app/providers.tsx`), che ha accesso al router e
 * alla cache delle query. Sparpagliare `window.location` nei moduli di rete
 * significa avere due sistemi di navigazione che non si conoscono.
 */
function expiredSessionError(): ApiError {
  return new ApiError(401, "unauthorized", "Sessione scaduta. Accedi di nuovo.");
}

async function parseError(response: Response): Promise<ApiError> {
  let code = "http_error";
  let message = "Si è verificato un errore.";
  let details: unknown;

  try {
    const body = (await response.json()) as Partial<ApiErrorBody>;
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      details = body.error.details;
    }
  } catch {
    // Risposta non JSON (proxy, gateway, 502 di piattaforma): si tiene il
    // messaggio generico. Il corpo grezzo non va mostrato all'utente.
    message = `Il server ha risposto con stato ${response.status}.`;
  }

  return new ApiError(response.status, code, message, details);
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Il corpo **e** lo status di una chiamata autenticata al backend.
 *
 * Ogni richiesta porta `Authorization: Bearer <jwt>`: il backend ricava lo
 * `user_id` dal token verificato e non accetta identità dal body.
 *
 * Esiste separata da `apiFetch` per un solo chiamante — `analyzeVideo`, che
 * deve distinguere 201 da 200 — e non è esportata proprio per questo: lo status
 * serve a chi ne fa qualcosa, e altrove sarebbe un dettaglio del trasporto che
 * si propaga senza motivo.
 *
 * Prima di questa separazione `analyzeVideo` non usava affatto l'helper e
 * riscriveva a mano cinque cose: il recupero del token, gli header, il `catch`
 * con `NetworkError` (con la stessa stringa di messaggio ricopiata), il ramo 401
 * e la chiamata a `parseError`. Cinque copie che dovevano restare allineate a
 * mano.
 */
async function apiFetchWithStatus<T>(
  path: string,
  { method = "GET", body, signal }: RequestOptions = {},
): Promise<{ data: T; status: number }> {
  const token = await getAccessToken();
  if (!token) throw expiredSessionError();

  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  let response: Response;
  try {
    response = await fetch(`${BACKEND_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new NetworkError(
      "Impossibile raggiungere il server. Verifica la connessione e che il backend sia attivo.",
    );
  }

  if (response.status === 401) throw expiredSessionError();

  if (!response.ok) {
    throw await parseError(response);
  }

  if (response.status === 204) {
    return { data: undefined as T, status: response.status };
  }

  return { data: (await response.json()) as T, status: response.status };
}

/** Il caso normale: solo il corpo, perché lo status non aggiunge nulla. */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return (await apiFetchWithStatus<T>(path, options)).data;
}

// =============================================================================
// Endpoint
// =============================================================================

export interface AnalyzeResult {
  insight: Insight;
  /** `true` quando il backend ha risposto 200: il video era già in cache. */
  fromCache: boolean;
}

/**
 * `POST /api/v1/analyze-video`.
 *
 * Il backend distingue **201** (analisi eseguita) da **200** (già presente in
 * cache), e la differenza serve alla UI per non far credere all'utente che sia
 * stata fatta un'analisi quando il record esisteva già. È l'unico endpoint del
 * frontend a cui lo status serva davvero.
 */
export async function analyzeVideo(payload: AnalyzeVideoPayload): Promise<AnalyzeResult> {
  const { data, status } = await apiFetchWithStatus<Insight>("/api/v1/analyze-video", {
    method: "POST",
    body: payload,
  });

  return { insight: data, fromCache: status === 200 };
}

/** `GET /api/v1/insights` con filtri e paginazione. */
export async function fetchInsights(
  query: InsightQuery,
  signal?: AbortSignal,
): Promise<InsightListResponse> {
  const params = new URLSearchParams();
  if (query.search?.trim()) params.set("search", query.search.trim());
  if (query.mode) params.set("mode", query.mode);
  if (query.creatorId) params.set("creator_id", query.creatorId);
  params.set("page", String(query.page ?? 1));
  params.set("limit", String(query.limit ?? 12));

  return apiFetch<InsightListResponse>(`/api/v1/insights?${params.toString()}`, { signal });
}

export async function fetchCreators(signal?: AbortSignal): Promise<CreatorListResponse> {
  return apiFetch<CreatorListResponse>("/api/v1/creators", { signal });
}

export async function createCreator(payload: CreatorCreatePayload): Promise<Creator> {
  return apiFetch<Creator>("/api/v1/creators", { method: "POST", body: payload });
}

export async function updateCreator(
  id: string,
  payload: CreatorUpdatePayload,
): Promise<Creator> {
  return apiFetch<Creator>(`/api/v1/creators/${id}`, { method: "PATCH", body: payload });
}

export async function deleteCreator(id: string): Promise<void> {
  await apiFetch<void>(`/api/v1/creators/${id}`, { method: "DELETE" });
}

/**
 * `POST /api/v1/creators/validate`.
 *
 * Chiama API esterne a consumo e ha una quota giornaliera per utente: va
 * invocata su un gesto concluso — blur, incolla — e **mai a ogni tasto**, che
 * significherebbe una chiamata pagata per lettera scritta.
 *
 * `signal` serve a scartare la risposta di una verifica superata dai fatti:
 * l'utente ha già cambiato quello che c'era scritto nel campo.
 */
export async function validateCreator(
  payload: CreatorValidationPayload,
  signal?: AbortSignal,
): Promise<CreatorValidation> {
  return apiFetch<CreatorValidation>("/api/v1/creators/validate", {
    method: "POST",
    body: payload,
    signal,
  });
}

// =============================================================================
// Messaggi d'errore per la UI
// =============================================================================

/**
 * Traduce un errore in un messaggio mostrabile.
 *
 * Il backend manda già messaggi scritti per l'utente finale e privi di dettagli
 * interni, quindi si usa il suo testo. I casi qui sotto sono quelli in cui il
 * contesto del frontend permette di essere più utili.
 */
export function toUserMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "video_too_large":
      case "video_too_long":
        return error.message;
      case "conflict":
        return "Questo creator è già nella tua watchlist.";
      case "validation_quota_reached":
        return "Hai esaurito le verifiche di account di oggi. Puoi comunque aggiungere il creator a mano.";
      case "youtube_unavailable":
        return "La verifica dei canali YouTube non è disponibile in questo momento.";
      case "gemini_unavailable":
        return "L'analisi non è disponibile in questo momento. Riprova tra qualche minuto.";
      case "apify_unavailable":
        return "Il recupero dei video dal creator non è disponibile in questo momento.";
      case "database_unavailable":
        return "Il servizio dati non è raggiungibile. Riprova tra poco.";
      default:
        return error.message;
    }
  }

  return "Si è verificato un errore imprevisto.";
}
