/**
 * Tipi allineati agli schemi Pydantic del backend.
 *
 * Fonte di verità: `backend/app/schemas/{analysis,creators,insights}.py`.
 * Se il backend cambia uno schema, questo file va aggiornato di conseguenza —
 * non c'è generazione automatica.
 *
 * Nota sui payload `jsonb`: il backend li rilegge come dizionari non tipizzati
 * (`dict[str, Any] | None`), perché contengono lo schema in vigore al momento
 * dell'analisi. Le interfacce qui sotto descrivono la forma *attuale*: un
 * record vecchio potrebbe non avere tutti i campi, quindi in UI vanno letti in
 * modo difensivo (optional chaining, fallback) e non dati per garantiti.
 */

export type AnalysisMode = "INFO" | "STYLE" | "BOTH";

export type Platform = "instagram" | "tiktok" | "youtube_shorts";

/** Filtro di `GET /insights`. `SCRIPT` non è una modalità di analisi: seleziona
 *  i record che *hanno* uno script inverso, a prescindere da `analysis_mode`. */
export type InsightFilterMode = AnalysisMode | "SCRIPT";

export type ContentFormat =
  | "tutorial"
  | "storytelling"
  | "listicle"
  | "opinion"
  | "review"
  | "demonstration"
  | "interview"
  | "reaction"
  | "promotional"
  | "other";

export type HookType =
  | "question"
  | "bold_claim"
  | "curiosity_gap"
  | "problem_agitation"
  | "story_opening"
  | "demonstration"
  | "shock_visual"
  | "direct_address"
  | "other";

export type Pacing = "slow" | "moderate" | "fast" | "very_fast";

export type BeatSection = "hook" | "context" | "development" | "payoff" | "cta";

/** Payload INFO — `insights.summary_data`. */
export interface InfoAnalysis {
  summary: string;
  main_topic: string;
  key_points: string[];
  target_audience: string;
  value_proposition: string;
  content_format: ContentFormat;
  keywords: string[];
}

/** Payload STYLE — `insights.style_data`. */
export interface StyleAnalysis {
  hook: string;
  hook_type: HookType;
  hook_duration_seconds: number;
  pacing: Pacing;
  average_shot_duration_seconds: number;
  editing_techniques: string[];
  visual_style: string;
  text_overlay_usage: string;
  audio_style: string;
  tone_of_voice: string;
  cta: string | null;
  retention_devices: string[];
}

export interface ScriptBeat {
  timestamp: string;
  section: BeatSection;
  content: string;
  purpose: string;
  visual_direction: string;
}

/** Script inverso riutilizzabile — `insights.inverse_script_template`. */
export interface InverseScript {
  title: string;
  total_duration_seconds: number;
  beats: ScriptBeat[];
  reusable_hook_template: string;
  reusable_cta_template: string | null;
  adaptation_notes: string;
}

export interface Insight {
  id: string;
  creator_id: string | null;
  user_id: string;
  video_url: string;
  thumbnail_url: string | null;
  analysis_mode: AnalysisMode;
  summary_data: InfoAnalysis | null;
  style_data: StyleAnalysis | null;
  inverse_script_template: InverseScript | null;
  keywords: string[];
  created_at: string;
}

export interface InsightListResponse {
  items: Insight[];
  page: number;
  limit: number;
  total: number;
  has_more: boolean;
}

export interface Creator {
  id: string;
  user_id: string;
  username: string;
  platform: Platform;
  analysis_mode: AnalysisMode;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreatorListResponse {
  items: Creator[];
  total: number;
}

export interface CreatorCreatePayload {
  username: string;
  platform: Platform;
  analysis_mode: AnalysisMode;
  is_active?: boolean;
}

/** Solo i due campi che il backend accetta in PATCH. */
export interface CreatorUpdatePayload {
  analysis_mode?: AnalysisMode;
  is_active?: boolean;
}

export interface AnalyzeVideoPayload {
  video_url: string;
  analysis_mode: AnalysisMode;
}

/** Envelope d'errore uniforme del backend (vedi `middleware/error_handler.py`). */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}

/** Dettaglio di un errore di validazione 422. */
export interface ValidationDetail {
  field: string;
  message: string;
  type: string;
}

export interface InsightQuery {
  search?: string;
  mode?: InsightFilterMode;
  creatorId?: string;
  page?: number;
  limit?: number;
}

/** Etichette leggibili per i valori enum che finiscono in UI. */
export const PLATFORM_LABELS: Record<Platform, string> = {
  instagram: "Instagram",
  tiktok: "TikTok",
  youtube_shorts: "YouTube Shorts",
};

export const PACING_LABELS: Record<Pacing, string> = {
  slow: "Lento",
  moderate: "Moderato",
  fast: "Veloce",
  very_fast: "Molto veloce",
};

export const HOOK_TYPE_LABELS: Record<HookType, string> = {
  question: "Domanda",
  bold_claim: "Affermazione forte",
  curiosity_gap: "Curiosity gap",
  problem_agitation: "Problema amplificato",
  story_opening: "Apertura narrativa",
  demonstration: "Dimostrazione",
  shock_visual: "Visual d'impatto",
  direct_address: "Rivolto allo spettatore",
  other: "Altro",
};

export const BEAT_SECTION_LABELS: Record<BeatSection, string> = {
  hook: "Hook",
  context: "Contesto",
  development: "Sviluppo",
  payoff: "Payoff",
  cta: "CTA",
};
