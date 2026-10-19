import type {
  Brief,
  CompareRow,
  ExtractBody,
  ThumbnailProfile,
  TryBody,
  TryRender,
  CreateProjectBody,
  CreateProjectResponse,
  Health,
  Job,
  Learning,
  LearningFit,
  Package,
  Plan,
  PlanEditBody,
  PlanRequestBody,
  ProjectDetail,
  ProjectSummary,
  RefineResponse,
  RefineStep,
  Render,
  Segment,
  Style,
  TimelineSummary,
  Track,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** FastAPI returns `detail` as a string, or as a list of validation errors on 422. */
function detailOf(data: unknown): string | null {
  if (!data || typeof data !== "object" || !("detail" in data)) return null;
  const detail = (data as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (d && typeof d === "object" && "msg" in d) {
          const loc = Array.isArray((d as { loc?: unknown }).loc)
            ? ((d as { loc: unknown[] }).loc.filter((p) => p !== "body").join("."))
            : "";
          return loc ? `${loc}: ${String((d as { msg: unknown }).msg)}` : String((d as { msg: unknown }).msg);
        }
        return String(d);
      })
      .join("; ");
  }
  return detail == null ? null : JSON.stringify(detail);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, `Server unreachable (${method} ${path})`);
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const fallback = typeof data === "string" && data.length < 300 ? data : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detailOf(data) ?? fallback);
  }
  return data as T;
}

/** For endpoints documented as "X or 404": resolve to null on 404. */
async function orNull<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

const enc = encodeURIComponent;

export const api = {
  health: () => request<Health>("GET", "/api/health"),
  styles: () => request<Style[]>("GET", "/api/styles"),
  music: () => request<Track[]>("GET", "/api/music"),
  importMusic: (folder: string) => request<Job>("POST", "/api/music/import", { folder }),

  listProjects: () => request<ProjectSummary[]>("GET", "/api/projects"),
  createProject: (body: CreateProjectBody) => request<CreateProjectResponse>("POST", "/api/projects", body),
  getProject: (id: string) => request<ProjectDetail>("GET", `/api/projects/${enc(id)}`),
  getSegments: (id: string) => request<Segment[]>("GET", `/api/projects/${enc(id)}/segments`),

  putBrief: (id: string, brief: Brief) => request<Brief>("PUT", `/api/projects/${enc(id)}/brief`, brief),

  startPlan: (id: string, body: PlanRequestBody = {}) =>
    request<Job>("POST", `/api/projects/${enc(id)}/plan`, body),
  getPlan: (id: string) => orNull(request<Plan>("GET", `/api/projects/${enc(id)}/plan`)),
  putPlan: (id: string, body: PlanEditBody) => request<Plan>("PUT", `/api/projects/${enc(id)}/plan`, body),

  startPreview: (id: string) => request<Job>("POST", `/api/projects/${enc(id)}/preview`),
  startMaster: (id: string) => request<Job>("POST", `/api/projects/${enc(id)}/master`),

  getTimeline: (id: string) => orNull(request<TimelineSummary>("GET", `/api/projects/${enc(id)}/timeline`)),
  getRenders: (id: string) => request<Render[]>("GET", `/api/projects/${enc(id)}/renders`),
  getPackage: (id: string) => orNull(request<Package>("GET", `/api/projects/${enc(id)}/package`)),

  getJob: (jobId: string) => request<Job>("GET", `/api/jobs/${enc(jobId)}`),

  // Phase 2
  extractStyle: (body: ExtractBody) => request<Job>("POST", "/api/styles/extract", body),
  deleteStyle: (id: string) => request<{ deleted: string }>("DELETE", `/api/styles/${enc(id)}`),
  compareStyles: (ids: string[]) =>
    request<CompareRow[]>("GET", `/api/styles/compare?ids=${ids.map(enc).join(",")}`),
  thumbnailProfiles: () => request<ThumbnailProfile[]>("GET", "/api/thumbnail-profiles"),
  extractThumbnailProfile: (body: ExtractBody) => request<Job>("POST", "/api/thumbnail-profiles/extract", body),
  startTry: (id: string, body: TryBody) => request<Job>("POST", `/api/projects/${enc(id)}/try`, body),
  getTry: (id: string) => request<TryRender[]>("GET", `/api/projects/${enc(id)}/try`),

  // Phase 3
  refine: (id: string, text: string) =>
    request<RefineResponse>("POST", `/api/projects/${enc(id)}/refine`, { text }),
  undo: (id: string) => request<RefineResponse>("POST", `/api/projects/${enc(id)}/undo`),
  refinements: (id: string) => request<RefineStep[]>("GET", `/api/projects/${enc(id)}/refinements`),
  learning: () => request<Learning>("GET", "/api/learning"),
  refit: () => request<LearningFit>("POST", "/api/learning/refit"),
  resetLearning: () => request<Learning>("DELETE", "/api/learning"),
};
