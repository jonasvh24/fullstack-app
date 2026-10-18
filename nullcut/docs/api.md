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
