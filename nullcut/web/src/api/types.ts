// Types copied from docs/api.md (Phase 1). Keep in sync with the contract.

export type JobStatus = "queued" | "running" | "done" | "failed";
// Phase 2 does not name the kinds of its new jobs (style/thumbnail extraction, try);
// any other string is accepted and labelled generically.
export type JobKind = "ingest" | "plan" | "preview" | "master" | "music_import" | "refine" | (string & {});

export interface Job {
  id: string;
  project_id: string;
  kind: JobKind;
  status: JobStatus;
  progress: number; // 0..1
  message: string; // human-readable current step, e.g. "a.mp4: transcribing"
  error: string | null;
  created: string;
  updated: string; // ISO 8601
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: any | null; // kind-specific
}

export interface Clip {
  id: string;
  name: string;
  duration: number;
  width: number;
  height: number;
  fps: number;
  language: string;
  asr: string;
  words: number; // count only; transcript via /segments
  speech: boolean; // false = B-roll candidate
  thumb_url: string | null; // /media/... jpg
  shots: number;
}

export interface Segment {
  id: string; // "c_ab12cd34ef56:s003"
  clip_id: string;
  start: number;
  end: number; // source seconds
  text: string;
  score: number; // highlight 0..1
  thumb_url: string | null;
}

export type MusicMode = "none" | "library" | "track" | "generate"; // Phase 3: generate
export type Energy = "low" | "mid" | "high";
export type CaptionMode = "off" | "clean" | "word";

export type Format = "16:9" | "9:16";
export type BriefFormat = Format | "both";

export interface Brief {
  text: string;
  target_s: number | null; // 15..1800
  tolerance: number; // default 0.05
  format: BriefFormat; // Phase 2: 9:16 and both enabled
  style: string; // style id from /api/styles
  music: { mode: MusicMode; mood: string; energy: Energy; track_id: string | null };
  captions: { mode: CaptionMode };
  extras: { zoom: number; broll: number; hook_required: boolean }; // 0..2 multipliers, 1 = style default
  thumbnail_profile: string; // Phase 2: id from /api/thumbnail-profiles
  title_refs: string; // Phase 2: reference titles, one per line (may be empty)
  // Phase 3: written by refinements, section -> key -> value. The UI shows, never edits.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  overrides?: Record<string, Record<string, any>>;
}

export type BeatRole = "hook" | "setup" | "body" | "payoff" | "cta";

export interface Beat {
  id: string;
  role: BeatRole;
  title: string;
  intent: string;
  segments: Segment[]; // in play order
  dropped: Segment[]; // chosen by planner but cut by the duration solver
  duration_s: number; // after jump cuts
  target_s: number | null;
  emphasis: string[];
  music_energy: Energy;
  locked: boolean;
}

export type SolverStatus = "met" | "short" | "over" | "no_target";

export interface Plan {
  version: number;
  planner: string; // "llm:<model>" | "heuristic"
  style: string;
  summary: string;
  questions: { id: string; text: string }[];
  beats: Beat[];
  solver: { target_s: number | null; kept_s: number; status: SolverStatus; shortfall_s: number };
}

export interface TimelineBeat {
  id: string;
  role: BeatRole;
  title: string;
  start_s: number;
  end_s: number;
}
export interface TimelineItem {
  start_s: number;
  end_s: number;
  clip_id: string;
  track: "V1" | "V2";
  rules: string[];
}
export interface TimelineZoom {
  start_s: number;
  end_s: number;
  scale: number;
  rule: string;
}
export interface TimelineMusic {
  track: string;
  start_s: number;
  end_s: number;
}

export interface TimelineSummary {
  version: number;
  fps: number;
  duration_s: number;
  width: number;
  height: number;
  beats: TimelineBeat[];
  items: TimelineItem[];
  zooms: TimelineZoom[];
  music: TimelineMusic | null;
}

/** Phase 2. score 0..1; note names the rule that missed when score < 0.8, else "". */
export interface MatchEntry {
  score: number;
  measured: number | string | null;
  wanted: number | string | null;
  note: string;
}

export interface Qc {
  length_ok: boolean | null;
  loudness_lufs: number | null;
  true_peak_dbtp: number | null;
  cuts_clean: boolean;
  worst_cut_db: number | null;
  style_match: Record<string, number>;
  style_detail?: Record<string, MatchEntry>; // Phase 2, measured on the rendered file
  style_overall?: number | null; // Phase 2, 0..1, mean of style_match; Phase 3: null while qc_pending
}

export interface Render {
  kind: "preview" | "master";
  version: number;
  url: string;
  duration_s: number;
  format: Format; // Phase 2
  qc: Qc;
  qc_pending?: boolean; // Phase 3: true while the preview plays and style_detail is still being measured
  encoded?: number | null; // Phase 3: items encoded for this render
  cached?: number | null; // Phase 3: items reused from the render cache
  audio_cached?: boolean | null; // Phase 3: true when the mix was reused
}

export interface TryRender {
  style: string;
  style_name: string;
  url: string;
  duration_s: number;
  match: Record<string, MatchEntry>;
  overall: number;
}

export interface Thumbnail {
  url: string;
  rank: number;
  score: number;
  text: string;
  layout: string;
  frame_t: number; // seconds into the master the face came from
  checks: {
    contrast_ratio: number;
    contrast_ok: boolean;
    face_px: number;
    face_ok: boolean;
    corner_clear: boolean;
    passed: boolean;
  };
}

export interface Package {
  titles: { text: string; rationale: string; verified: boolean }[];
  description: string;
  chapters: { t: number; title: string }[];
  thumbnails: Thumbnail[]; // Phase 2: three, ranked
  files: {
    mp4_vertical: string | null;
    srt: string;
    vtt: string;
    otio: string;
    fcpxml: string | null;
    cube?: string | null; // Phase 3: the grade as a 3D LUT
  }; // /media URLs
  export_checks?: { fcpxml?: ExportCheck } | null; // Phase 3
}

/** Phase 3. A failed export carries only ok, errors and warnings. */
export interface ExportCheck {
  ok: boolean;
  errors: string[];
  warnings: string[];
  spine_clips?: number;
  connected_clips?: number;
  duration_s?: number;
  fps?: number;
  roundtrip?: {
    read: boolean;
    clips?: number;
    expected?: number;
    durations_match?: boolean;
    broll?: number;
    broll_expected?: number;
  };
}

// Phase 3: chat refinement

/** One parameter change, e.g. label "zooms per minute 12 → 6". */
export interface Change {
  param: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  from: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  to: any;
  label: string;
}

export type RefineKind = "tweak" | "structural" | "undo";

export interface RefineStep {
  id: number;
  at: string; // ISO 8601
  instruction: string; // what the creator typed
  kind: RefineKind;
  by: string; // "rules" or "llm:<model>"
  summary: string;
  changes: Change[]; // tweaks only
  unmapped: string[]; // clauses that were not understood (the rest was applied)
  labelled: number; // examples recorded for the learned weights
  undone: boolean;
  undoes: number | null;
  plan_version: number;
  render: { version: number; items: number; encoded: number; cached: number; audio_remixed: boolean } | null;
  timing: {
    interpret_s: number;
    apply_s: number;
    queued_s?: number;
    render_s: number;
    to_preview_s: number;
    qc_s?: number;
  } | null;
}

export interface RefineResponse {
  step: RefineStep | null; // null when the instruction was not understood
  message: string | null; // set when step is null
  job: Job | null; // kind "refine"; job.result may hold RefineResult while still running
}

/** The refine job's result: present early (preview playable, QC pending) and final (QC filled in). */
export interface RefineResult {
  step: RefineStep;
  render: Render;
}

// Phase 3: learned highlight weights

export interface LearningFit {
  fitted_at: string;
  examples: number;
  explicit: number;
  adopted: boolean;
  reason?: string;
  bias?: number;
  coef?: Record<string, number>;
  cv?: {
    learned_auc: number; // adoption is decided on held-out AUC
    hand_auc: number;
    learned_logloss: number;
    hand_logloss: number;
    learned_accuracy: number;
    hand_accuracy: number;
  };
}

export interface Learning {
  examples: number;
  explicit: number;
  kept: number;
  deleted: number;
  projects: number;
  min_examples: number; // explicit choices needed before a refit can adopt
  hand_weights: Record<string, number>; // feature -> weight
  last_fit: LearningFit | null;
  active: "hand" | "learned";
  new_since_fit: boolean;
  next_refit: string | null; // ISO local time of the nightly refit, null when disabled
}

export interface Style {
  id: string;
  name: string;
  description: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params: Record<string, any>;
  kind: "shipped" | "extracted"; // Phase 2
  sources: string[]; // Phase 2: reference file names, extracted styles only
}

export interface ThumbnailProfile {
  id: string;
  name: string;
  description: string;
  kind: "shipped" | "extracted";
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params: Record<string, any>;
}

export interface CompareRow {
  key: string;
  label: string;
  unit: string;
  min: number;
  max: number;
  values: Record<string, number | null>; // style id -> value
}

export interface Track {
  id: string;
  name: string;
  duration: number;
  bpm: number;
  energy: number;
  tags: string[];
  licence: string;
}

// Endpoint shapes (named here; inline in the contract's endpoint table).

export interface Health {
  version: string;
  llm: { url: string; model: string; ok: boolean };
  vlm: { url: string; ok: boolean };
  asr: string;
  encoder: string;
  musicgen?: { backend: "builtin" | "command" | (string & {}); licence: string }; // Phase 3
}

export interface ProjectSummary {
  id: string;
  name: string;
  source_dir: string;
  clips: number;
  duration: number;
  updated: string;
}

export interface ProjectDetail {
  id: string;
  name: string;
  source_dir: string;
  clips: Clip[];
  brief: Brief;
  jobs: Job[];
  plan_version: number | null;
  timeline_version: number | null;
}

export interface CreateProjectBody {
  source_dir: string;
  name?: string;
}

// The contract does not pin the shape of `project` here; only `id` is relied upon.
export interface CreateProjectResponse {
  project: { id: string; name?: string; source_dir?: string };
  job: Job;
}

export interface PlanRequestBody {
  answers?: Record<string, string>;
}

export interface PlanEditBody {
  beats: { id: string; locked: boolean; segment_ids: string[] }[];
}

export interface MasterResult {
  render: Render;
  package: Package;
}

export interface ExtractBody {
  name: string;
  folder: string;
}

export interface TryBody {
  styles: string[]; // 2..4
  seconds?: number;
}

export interface TryResult {
  renders: TryRender[];
}
