import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, errorMessage } from "../api/client";
import type { Job, Plan, ProjectDetail, Render } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { StepBar, type Step, type StepState } from "../components/StepBar";
import { isActive, latestOfKind, mergeJobs } from "../lib/jobs";
import { href } from "../lib/router";
import { usePoll } from "../lib/usePoll";
import { BriefPanel } from "../panels/BriefPanel";
import { ClipsPanel } from "../panels/ClipsPanel";
import { DeliverPanel } from "../panels/DeliverPanel";
import { PreviewPanel } from "../panels/PreviewPanel";
import { StoryboardPanel } from "../panels/StoryboardPanel";

/** Everything a step panel needs from the project page. */
export interface ProjectContext {
  project: ProjectDetail;
  jobs: Job[];
  plan: Plan | null;
  renders: Render[];
  setPlan: (plan: Plan) => void;
  setProject: (fn: (p: ProjectDetail) => ProjectDetail) => void;
  onJobStarted: (job: Job) => void;
  reload: () => Promise<void>;
  /** Refetch renders now, e.g. when a refinement's preview becomes playable before its job ends. */
  refreshRenders: () => Promise<void>;
}

export function ProjectPage({ id, step }: { id: string; step: Step }) {
  const [project, setProjectState] = useState<ProjectDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Jobs started from this page, polled directly via /api/jobs/{id} as well as through the project.
  const [watched, setWatched] = useState<Record<string, Job>>({});
  const watchedRef = useRef(watched);
  watchedRef.current = watched;
  const [plan, setPlan] = useState<Plan | null>(null);
  const [renders, setRenders] = useState<Render[]>([]);
  const [sideError, setSideError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setProjectState(await api.getProject(id));
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, [id]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const jobs = useMemo(() => mergeJobs(project?.jobs ?? [], Object.values(watched)), [project?.jobs, watched]);
  const anyActive = jobs.some(isActive);

  usePoll(
    async () => {
      const activeWatched = Object.values(watchedRef.current).filter(isActive);
      const [p, fresh] = await Promise.all([
        api.getProject(id),
        Promise.all(
          activeWatched.map((j) =>
            api.getJob(j.id).catch((e: unknown): Job => {
              // A job the server no longer knows is finished for our purposes; transient errors keep it polled.
              if (e instanceof ApiError && e.status === 404) {
                return { ...j, status: "failed", error: "Job no longer reported by the server." };
              }
              return j;
            }),
          ),
        ),
      ]);
      setProjectState(p);
      setLoadError(null);
      if (fresh.length) setWatched((prev) => ({ ...prev, ...Object.fromEntries(fresh.map((j) => [j.id, j])) }));
    },
    anyActive,
    1000,
  );

  // When the last running job settles, refresh once so versions and renders are current.
  const wasActive = useRef(false);
  useEffect(() => {
    if (wasActive.current && !anyActive) void reload();
    wasActive.current = anyActive;
  }, [anyActive, reload]);

  // Renders: refetch whenever the set of finished jobs changes.
  const doneKey = jobs
    .filter((j) => j.status === "done")
    .map((j) => j.id)
    .sort()
    .join(",");
  useEffect(() => {
    let cancelled = false;
    api
      .getRenders(id)
      .then((r) => {
        if (!cancelled) setRenders(r);
      })
      .catch((e) => {
        if (!cancelled) setSideError(`Renders: ${errorMessage(e)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [id, doneKey]);

  // Plan: refetch when the project's plan version changes.
  const planVersion = project?.plan_version ?? null;
  useEffect(() => {
    if (planVersion == null) {
      setPlan(null);
      return;
    }
    let cancelled = false;
    api
      .getPlan(id)
      .then((p) => {
        if (!cancelled) setPlan(p);
      })
      .catch((e) => {
        if (!cancelled) setSideError(`Plan: ${errorMessage(e)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [id, planVersion]);

  const refreshRenders = useCallback(async () => {
    try {
      setRenders(await api.getRenders(id));
    } catch (e) {
      setSideError(`Renders: ${errorMessage(e)}`);
    }
  }, [id]);

  const onJobStarted = useCallback((job: Job) => {
    setWatched((prev) => ({ ...prev, [job.id]: job }));
  }, []);

  const setProject = useCallback((fn: (p: ProjectDetail) => ProjectDetail) => {
    setProjectState((p) => (p ? fn(p) : p));
  }, []);

  const acceptPlan = useCallback(
    (p: Plan) => {
      setPlan(p);
      setProjectState((prev) => (prev ? { ...prev, plan_version: p.version } : prev));
    },
    [],
  );

  if (!project) {
    return (
      <div className="page">
        <p>
          <a href={href("/")}>Projects</a>
        </p>
        {loadError ? <ErrorText error={loadError} /> : <p className="dim">Loading project</p>}
      </div>
    );
  }

  const hasPlan = project.plan_version != null;
  const hasPreview = renders.some((r) => r.kind === "preview") || project.timeline_version != null;
  const busy = (kind: Job["kind"]) => isActive(latestOfKind(jobs, kind));
  const states: Record<Step, StepState> = {
    clips: { available: true, busy: busy("ingest") },
    brief: { available: true },
    storyboard: { available: hasPlan, reason: "needs a plan", busy: busy("plan") },
    preview: { available: hasPreview, reason: "needs a render", busy: busy("preview") || busy("refine") },
    deliver: { available: hasPlan, reason: "needs a plan", busy: busy("master") },
  };

  const ctx: ProjectContext = {
    project,
    jobs,
    plan,
    renders,
    setPlan: acceptPlan,
    setProject,
    onJobStarted,
    reload,
    refreshRenders,
  };

  return (
    <div className="page page-project">
      <div className="project-head">
        <div className="project-title">
          <a className="crumb" href={href("/")}>
            Projects
          </a>
          <span className="crumb-sep" aria-hidden="true">
            /
          </span>
          <h1 className="page-title">{project.name}</h1>
          <span className="mono dim truncate" title={project.source_dir}>
            {project.source_dir}
          </span>
        </div>
        <StepBar projectId={project.id} current={step} states={states} />
      </div>
      {loadError && <ErrorText error={`Refresh failed: ${loadError}`} />}
      {sideError && <ErrorText error={sideError} />}
      <div className="step-body">
        {step === "clips" && <ClipsPanel ctx={ctx} />}
        {step === "brief" && <BriefPanel ctx={ctx} />}
        {step === "storyboard" && <StoryboardPanel ctx={ctx} />}
        {step === "preview" && <PreviewPanel ctx={ctx} />}
        {step === "deliver" && <DeliverPanel ctx={ctx} />}
      </div>
    </div>
  );
}
