// Types copied from docs/api.md (Phase 1). Keep in sync with the contract.

export type JobStatus = "queued" | "running" | "done" | "failed";
export type JobKind = "ingest" | "plan" | "preview" | "master" | "music_import";

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

export interface Brief {
  text: string;
  target_s: number | null; // 15..1800
  tolerance: number; // default 0.05
  format: "16:9"; // 9:16 arrives in Phase 2
  style: string; // style id from /api/styles
  music: { mode: MusicMode; mood: string; energy: Energy; track_id: string | null };
  captions: { mode: CaptionMode };
  extras: { zoom: number; broll: number; hook_required: boolean }; // 0..2 multipliers, 1 = style default
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

export interface Qc {
  length_ok: boolean | null;
  loudness_lufs: number | null;
  true_peak_dbtp: number | null;
  cuts_clean: boolean;
  worst_cut_db: number | null;
  style_match: Record<string, number>;
}

export interface Render {
  kind: "preview" | "master";
  version: number;
  url: string;
  duration_s: number;
  qc: Qc;
}

export interface Package {
  titles: { text: string; rationale: string; verified: boolean }[];
  description: string;
  chapters: { t: number; title: string }[];
  files: { srt: string; vtt: string; otio: string; fcpxml: string | null }; // /media URLs
}

export interface Style {
  id: string;
  name: string;
  description: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params: Record<string, any>;
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
