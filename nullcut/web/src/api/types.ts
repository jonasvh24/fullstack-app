// Types copied from docs/api.md (Phase 1). Keep in sync with the contract.

export type JobStatus = "queued" | "running" | "done" | "failed";
// Phase 2 does not name the kinds of its new jobs (style/thumbnail extraction, try);
// any other string is accepted and labelled generically.
export type JobKind = "ingest" | "plan" | "preview" | "master" | "music_import" | (string & {});

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

export type MusicMode = "none" | "library" | "track";
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
  style_overall?: number; // Phase 2, 0..1, mean of style_match
}

export interface Render {
  kind: "preview" | "master";
  version: number;
  url: string;
  duration_s: number;
  format: Format; // Phase 2
  qc: Qc;
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
  files: { mp4_vertical: string | null; srt: string; vtt: string; otio: string; fcpxml: string | null }; // /media URLs
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
