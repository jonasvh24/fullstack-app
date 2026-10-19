# nullcut HTTP API (Phase 1)

FastAPI on `http://127.0.0.1:8765`. JSON everywhere. The built UI is served from `/`; the API lives under `/api`; project files under `/media`.

Long operations return a **job** immediately and run in the background. The UI polls `GET /api/jobs/{job_id}` (or the project's job list) about once a second.

## Types

```ts
type JobStatus = "queued" | "running" | "done" | "failed";
interface Job {
  id: string; project_id: string;
  kind: "ingest" | "plan" | "preview" | "master" | "music_import";
  status: JobStatus;
  progress: number;          // 0..1
  message: string;           // human-readable current step, e.g. "a.mp4: transcribing"
  error: string | null;
  created: string; updated: string;   // ISO 8601
  result: any | null;        // kind-specific, see below
}

interface Clip {
  id: string; name: string; duration: number; width: number; height: number; fps: number;
  language: string; asr: string;
  words: number;             // count only; transcript via /segments
  speech: boolean;           // false = B-roll candidate
  thumb_url: string | null;  // /media/... jpg
  shots: number;
}

interface Segment {          // a sentence unit, the smallest piece a plan places
  id: string;                // "c_ab12cd34ef56:s003"
  clip_id: string; start: number; end: number;   // source seconds
  text: string; score: number;                    // highlight 0..1
  thumb_url: string | null;
}

interface Brief {
  text: string;                               // free text, e.g. "fast travel vlog, open on the cliff jump"
  target_s: number | null;                    // 15..1800
  tolerance: number;                          // default 0.05
  format: "16:9";                             // 9:16 arrives in Phase 2
  style: string;                              // style id from /api/styles
  music: { mode: "none" | "library" | "track"; mood: string; energy: "low" | "mid" | "high"; track_id: string | null };
  captions: { mode: "off" | "clean" | "word" };
  extras: { zoom: number; broll: number; hook_required: boolean };  // zoom, broll: 0..2 multipliers, 1 = style default
}

type BeatRole = "hook" | "setup" | "body" | "payoff" | "cta";
interface Beat {
  id: string; role: BeatRole; title: string; intent: string;
  segments: Segment[];         // in play order
  dropped: Segment[];          // chosen by planner but cut by the duration solver
  duration_s: number;          // after jump cuts
  target_s: number | null;
  emphasis: string[];          // words to punch
  music_energy: "low" | "mid" | "high";
  locked: boolean;
}

interface Plan {
  version: number; planner: string;   // "llm:<model>" | "heuristic"
  style: string; summary: string;
  questions: { id: string; text: string }[];   // non-empty => planner wants answers before render
  beats: Beat[];
  solver: { target_s: number | null; kept_s: number; status: "met" | "short" | "over" | "no_target"; shortfall_s: number };
}

interface TimelineSummary {
  version: number; fps: number; duration_s: number; width: number; height: number;
  beats: { id: string; role: BeatRole; title: string; start_s: number; end_s: number }[];
  items: { start_s: number; end_s: number; clip_id: string; track: "V1" | "V2"; rules: string[] }[];
  zooms: { start_s: number; end_s: number; scale: number; rule: string }[];
  music: { track: string; start_s: number; end_s: number } | null;
}

interface Render {
  kind: "preview" | "master"; version: number; url: string; duration_s: number;
  qc: { length_ok: boolean | null; loudness_lufs: number | null; true_peak_dbtp: number | null;
        cuts_clean: boolean; worst_cut_db: number | null; style_match: Record<string, number> };
}

interface Package {
  titles: { text: string; rationale: string; verified: boolean }[];
  description: string;
  chapters: { t: number; title: string }[];
  files: { srt: string; vtt: string; otio: string; fcpxml: string | null };   // /media URLs
}

interface Style { id: string; name: string; description: string; params: Record<string, any> }
interface Track { id: string; name: string; duration: number; bpm: number; energy: number; tags: string[]; licence: string }
```

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/health` | | `{version, llm: {url, model, ok}, vlm: {url, ok}, asr: string, encoder: string}` |
| GET | `/api/styles` | | `Style[]` |
| GET | `/api/music` | | `Track[]` |
| POST | `/api/music/import` | `{folder}` | `Job` |
| GET | `/api/projects` | | `{id, name, source_dir, clips: number, duration: number, updated: string}[]` |
| POST | `/api/projects` | `{source_dir, name?}` | `{project, job}` (ingest starts at once) |
| GET | `/api/projects/{id}` | | `{id, name, source_dir, clips: Clip[], brief: Brief, jobs: Job[], plan_version: number \| null, timeline_version: number \| null}` |
| GET | `/api/projects/{id}/segments` | | `Segment[]` (all, story order) |
| PUT | `/api/projects/{id}/brief` | `Brief` | `Brief` |
| POST | `/api/projects/{id}/plan` | `{answers?: Record<string,string>}` | `Job` (result: `Plan`) |
| GET | `/api/projects/{id}/plan` | | `Plan` or 404 |
| PUT | `/api/projects/{id}/plan` | `{beats: {id, locked, segment_ids}[]}` | `Plan` (new version; order = play order; omitted beats are deleted) |
| POST | `/api/projects/{id}/preview` | | `Job` (result: `Render`) |
| POST | `/api/projects/{id}/master` | | `Job` (result: `{render: Render, package: Package}`) |
| GET | `/api/projects/{id}/timeline` | | `TimelineSummary` or 404 |
| GET | `/api/projects/{id}/renders` | | `Render[]` newest first |
| GET | `/api/projects/{id}/package` | | `Package` or 404 |
| GET | `/api/jobs/{job_id}` | | `Job` |
| GET | `/media/{project_id}/{path}` | | file, with HTTP Range support for video |

Errors: `{detail: string}` with 4xx/5xx.

## Phase 2 additions

### Types

```ts
type Format = "16:9" | "9:16";

interface Style {                     // extends Phase 1
  id: string; name: string; description: string; params: Record<string, any>;
  kind: "shipped" | "extracted";
  sources: string[];                  // reference file names, extracted styles only
}

interface MatchEntry { score: number; measured: number | string | null; wanted: number | string | null; note: string }
// score 0..1; note names the rule that missed when score < 0.8, else "".

interface Brief {                     // Phase 1 fields plus:
  format: "16:9" | "9:16" | "both";   // 9:16 now enabled
  thumbnail_profile: string;          // id from /api/thumbnail-profiles
  title_refs: string;                 // reference titles, one per line (may be empty)
}

interface Render {                    // Phase 1 fields plus:
  format: Format;
  qc: { /* Phase 1 fields */ style_match: Record<string, number>;
        style_detail: Record<string, MatchEntry>;   // measured on the rendered file
        style_overall: number };                    // 0..1, mean of style_match
}

interface TryRender { style: string; style_name: string; url: string; duration_s: number;
                      match: Record<string, MatchEntry>; overall: number }

interface Thumbnail {
  url: string; rank: number; score: number; text: string; layout: string;
  frame_t: number;                    // seconds into the master the face came from
  checks: { contrast_ratio: number; contrast_ok: boolean; face_px: number; face_ok: boolean;
            corner_clear: boolean; passed: boolean };
}

interface Package {                   // Phase 1 fields plus:
  thumbnails: Thumbnail[];            // three, ranked
  files: { /* Phase 1 */ mp4_vertical: string | null; srt: string; vtt: string; otio: string; fcpxml: string | null };
}

interface ThumbnailProfile { id: string; name: string; description: string; kind: "shipped" | "extracted"; params: Record<string, any> }

interface CompareRow { key: string; label: string; unit: string; min: number; max: number;
                       values: Record<string, number | null> }     // style id -> value
```

### Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/styles/extract` | `{name, folder}` (3–10 reference videos on the server) | `Job` (result: `Style`) |
| DELETE | `/api/styles/{id}` | | `{deleted: id}` (extracted styles only; 400 for shipped) |
| GET | `/api/styles/compare?ids=a,b,c` | | `CompareRow[]` (2–4 ids; same axes for every style) |
| GET | `/api/thumbnail-profiles` | | `ThumbnailProfile[]` |
| POST | `/api/thumbnail-profiles/extract` | `{name, folder}` (20–50 reference thumbnails) | `Job` (result: `ThumbnailProfile`) |
| POST | `/api/projects/{id}/try` | `{styles: string[] (2–4), seconds?: number}` | `Job` (result: `{renders: TryRender[]}`) |
| GET | `/api/projects/{id}/try` | | `TryRender[]` (latest try run; empty array if none) |

`POST /api/projects/{id}/preview` and `/master` render every format the brief asks for. `GET /api/projects/{id}/renders` lists all of them with `format`. The master job's result keeps the Phase 1 shape: `{render, package}`, where `render` is the 16:9 one (or the 9:16 one when the brief is 9:16 only), and `package.thumbnails` holds three ranked thumbnails.

## Phase 3 additions

Chat refinement, learned highlight weights, generated music, exports for Resolve.

### Types

```ts
interface Brief {                     // Phase 2 fields plus:
  music: { mode: "none" | "library" | "track" | "generate"; mood: string; energy: "low" | "mid" | "high"; track_id: string | null };
  overrides: Record<string, Record<string, any>>;   // written by refinements; the UI shows, never edits
}

interface Render {                    // Phase 2 fields plus:
  qc_pending: boolean;                // true: the preview plays, style_detail is still being measured
  qc: { /* ... */ style_overall: number | null };   // null while qc_pending
  encoded: number | null;             // items encoded for this render
  cached: number | null;              // items reused from the render cache
  audio_cached: boolean | null;       // true when the mix was reused
}

interface Change { param: string; from: any; to: any; label: string }   // "zooms per minute 12 → 6"

interface RefineStep {
  id: number; at: string;             // ISO 8601
  instruction: string;                // what the creator typed
  kind: "tweak" | "structural" | "undo";
  by: "rules" | string;               // "rules" or "llm:<model>"
  summary: string;                    // one line: what changed
  changes: Change[];                  // tweaks only
  unmapped: string[];                 // clauses that were not understood (the rest was applied)
  labelled: number;                   // examples recorded for the learned weights
  undone: boolean; undoes: number | null;
  plan_version: number;
  render: { version: number; items: number; encoded: number; cached: number;
            audio_remixed: boolean } | null;   // null until the preview is rendered
  timing: { interpret_s: number; apply_s: number; queued_s?: number; render_s: number;
            to_preview_s: number; qc_s?: number } | null;
}

interface RefineResponse {
  step: RefineStep | null;            // null when the instruction was not understood
  message: string | null;             // set when step is null: why, with examples to try
  job: Job | null;                    // kind "refine". While running, job.result may already hold
                                      // {step, render}: the preview is playable then; QC follows.
                                      // Final result: {step, render} with QC filled in.
}

interface Learning {
  examples: number; explicit: number; kept: number; deleted: number; projects: number;
  min_examples: number;               // explicit choices needed before a refit can adopt
  hand_weights: Record<string, number>;           // feature -> weight
  last_fit: null | { fitted_at: string; examples: number; explicit: number; adopted: boolean;
                     reason?: string; bias?: number; coef?: Record<string, number>;
                     cv?: { learned_auc: number; hand_auc: number;   // adoption is decided on AUC
                            learned_logloss: number; hand_logloss: number;
                            learned_accuracy: number; hand_accuracy: number } };
  active: "hand" | "learned";
  new_since_fit: boolean;
  next_refit: string | null;          // ISO local time of the nightly refit, null when disabled
}

interface ExportCheck {
  ok: boolean; version?: string;      // "1.8"
  errors: string[]; warnings: string[];   // other fields absent when the export itself failed
  spine_clips: number; connected_clips: number; duration_s: number; fps: number;
  roundtrip: { read: boolean; clips?: number; expected?: number; durations_match?: boolean;
               broll?: number; broll_expected?: number };
}

interface Package {                   // Phase 2 fields plus:
  files: { /* ... */ cube: string | null };   // the grade as a 3D LUT
  export_checks: { fcpxml: ExportCheck } | null;
}
```

`GET /api/health` adds `musicgen: {backend: "builtin" | "command", licence: string}`. Its `llm.url` and `llm.model` are `null` when no LLM is configured.

The refine job's running and final `result` is `{step: RefineStep, render: Render}`; `render` is the first format, the 9:16 one of a `both` brief is in `GET /renders`.

### Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/projects/{id}/refine` | `{text}` | `RefineResponse`; 400 `{detail}` when it cannot apply (a locked beat, no matching sentence: the detail says which); 409 while a refinement is rendering |
| POST | `/api/projects/{id}/undo` | | `RefineResponse` (kind `undo`) |
| GET | `/api/projects/{id}/refinements` | | `RefineStep[]`, oldest first |
| GET | `/api/learning` | | `Learning` |
| POST | `/api/learning/refit` | | `Learning["last_fit"]` (synchronous, well under a second) |
| DELETE | `/api/learning` | | `Learning` (examples and weights removed; back to hand weights) |

Refinement examples the rules understand without an LLM (English and Dutch): "less zoom", "no B-roll", "music quieter", "generate the music", "warmer", "black and white", "word by word captions", "highlight in red", "bigger captions", "punchier first 15 seconds", "slower", "make it 2:30", "30 seconds shorter", "vertical version too", "switch to the documentary style", "drop the part about pricing", "remove beat 3", "bring back the line about the lake", "open on the sunset instead", "end with the thank-you line", "move X before Y", "swap beats 2 and 3", "shorten the setup", "undo". Anything else goes to the LLM when one is reachable.
