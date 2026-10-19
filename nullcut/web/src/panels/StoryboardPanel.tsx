import { useEffect, useMemo, useState, type DragEvent } from "react";
import { api, errorMessage } from "../api/client";
import type { Beat, Clip, Plan, PlanEditBody } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { JobProgress } from "../components/JobProgress";
import { seconds, tc } from "../lib/format";
import { isActive, latestOfKind, useJobDone } from "../lib/jobs";
import { href, navigate } from "../lib/router";
import type { ProjectContext } from "../pages/ProjectPage";
import { BeatCard, type DropPos, type LocalBeat } from "./BeatCard";
import { QuestionsPanel } from "./QuestionsPanel";
import { TryStyles } from "./TryStyles";

function toLocal(plan: Plan): LocalBeat[] {
  return plan.beats.map((b) => ({ ...b, removed: [], edited: false }));
}

function editBody(beats: Pick<Beat, "id" | "locked" | "segments">[]): PlanEditBody {
  return { beats: beats.map((b) => ({ id: b.id, locked: b.locked, segment_ids: b.segments.map((s) => s.id) })) };
}

function moveItem<T>(list: T[], from: number, insertAt: number): T[] {
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(insertAt > from ? insertAt - 1 : insertAt, 0, item);
  return next;
}

const STATUS_TEXT: Record<Plan["solver"]["status"], string> = {
  met: "met",
  short: "short",
  over: "over",
  no_target: "no target",
};

function SolverStrip({ plan, tolerance, stale }: { plan: Plan; tolerance: number; stale: boolean }) {
  const s = plan.solver;
  const delta = s.target_s != null ? s.kept_s - s.target_s : null;
  return (
    <div className={`solver solver-${s.status}`} role="status" aria-label="Length solver">
      <div className="solver-cell">
        <span className="solver-k">Kept</span>
        <span className="solver-v mono">{tc(s.kept_s)}</span>
      </div>
      <div className="solver-cell">
        <span className="solver-k">Target</span>
        <span className="solver-v mono">
          {s.target_s != null ? tc(s.target_s) : "–"}
          {s.target_s != null && <span className="dim"> ±{Math.round(tolerance * 100)}%</span>}
        </span>
      </div>
      <div className="solver-cell">
        <span className="solver-k">Status</span>
        <span className={`solver-status st-${s.status}`}>{STATUS_TEXT[s.status]}</span>
      </div>
      <div className="solver-cell">
        <span className="solver-k">{s.status === "over" ? "Over by" : "Shortfall"}</span>
        <span className="solver-v mono">
          {s.status === "over" && delta != null ? seconds(delta) : s.shortfall_s > 0 ? seconds(s.shortfall_s) : "0.0 s"}
        </span>
      </div>
      <div className="solver-cell solver-meta">
        <span className="mono dim">
          plan v{plan.version} · {plan.planner} · {plan.style}
        </span>
        {stale && <span className="warn-text">Unsaved edits. Figures reflect the last save.</span>}
      </div>
    </div>
  );
}

export function StoryboardPanel({ ctx }: { ctx: ProjectContext }) {
  const { project, plan, jobs, onJobStarted, setPlan } = ctx;
  const pid = encodeURIComponent(project.id);

  const [beats, setBeats] = useState<LocalBeat[]>(() => (plan ? toLocal(plan) : []));
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [over, setOver] = useState<{ index: number; pos: DropPos } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [answerError, setAnswerError] = useState<string | null>(null);

  // Reset local edits whenever a new plan version arrives.
  useEffect(() => {
    if (plan) setBeats(toLocal(plan));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan?.version]);

  const clips = useMemo(() => new Map<string, Clip>(project.clips.map((c) => [c.id, c])), [project.clips]);
  const original = useMemo(() => new Map((plan?.beats ?? []).map((b) => [b.id, b])), [plan]);

  const planJob = latestOfKind(jobs, "plan");
  const previewJob = latestOfKind(jobs, "preview");
  const planning = isActive(planJob);
  const rendering = isActive(previewJob);
  useJobDone(previewJob, () => navigate(`/p/${pid}/preview`));

  if (!plan) {
    return (
      <section className="panel">
        <h2 className="section-title">Storyboard</h2>
        {planJob && <JobProgress job={planJob} />}
        <p className="empty">
          No plan yet. <a href={href(`/p/${pid}/brief`)}>Plan the edit from the brief.</a>
        </p>
      </section>
    );
  }

  const dirty = JSON.stringify(editBody(beats)) !== JSON.stringify(editBody(plan.beats));
  const locked = saving || planning || rendering;

  const update = (id: string, fn: (b: LocalBeat) => LocalBeat) =>
    setBeats((list) => list.map((b) => (b.id === id ? fn(b) : b)));

  const removeSegment = (beatId: string, segId: string) =>
    update(beatId, (b) => {
      const seg = b.segments.find((s) => s.id === segId);
      return seg ? { ...b, segments: b.segments.filter((s) => s.id !== segId), removed: [...b.removed, seg] } : b;
    });

  const restoreSegment = (beatId: string, segId: string) =>
    update(beatId, (b) => {
      const seg = b.removed.find((s) => s.id === segId);
      if (!seg) return b;
      // Put it back in its original play position.
      const order = (original.get(beatId)?.segments ?? []).map((s) => s.id);
      const segments = [...b.segments, seg].sort((x, y) => order.indexOf(x.id) - order.indexOf(y.id));
      return { ...b, segments, removed: b.removed.filter((s) => s.id !== segId) };
    });

  const save = async (): Promise<boolean> => {
    setSaveError(null);
    setSaving(true);
    try {
      setPlan(await api.putPlan(project.id, editBody(beats)));
      return true;
    } catch (e) {
      setSaveError(errorMessage(e));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const renderPreview = async () => {
    setRenderError(null);
    if (dirty && !(await save())) return;
    try {
      onJobStarted(await api.startPreview(project.id));
    } catch (e) {
      setRenderError(errorMessage(e));
    }
  };

  const answer = async (answers: Record<string, string>) => {
    setAnswerError(null);
    try {
      onJobStarted(await api.startPlan(project.id, { answers }));
    } catch (e) {
      setAnswerError(errorMessage(e));
    }
  };

  // HTML5 drag and drop.
  const onDragStart = (i: number) => (e: DragEvent) => {
    setDragFrom(i);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", beats[i].id);
  };
  const onDragOver = (i: number) => (e: DragEvent) => {
    if (dragFrom == null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const pos: DropPos = e.clientY < rect.top + rect.height / 2 ? "before" : "after";
    if (!over || over.index !== i || over.pos !== pos) setOver({ index: i, pos });
  };
  const onDrop = (i: number) => (e: DragEvent) => {
    e.preventDefault();
    if (dragFrom != null && over) {
      const insertAt = over.pos === "before" ? i : i + 1;
      if (insertAt !== dragFrom && insertAt !== dragFrom + 1) setBeats((list) => moveItem(list, dragFrom, insertAt));
    }
    setDragFrom(null);
    setOver(null);
  };
  const onDragEnd = () => {
    setDragFrom(null);
    setOver(null);
  };

  const questions = plan.questions ?? [];

  return (
    <section className="panel" aria-labelledby="sb-title">
      <div className="panel-head sticky-head">
        <h2 id="sb-title" className="section-title">
          Storyboard
        </h2>
        <span className="mono dim">{beats.length} beats</span>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => setBeats(toLocal(plan))} disabled={!dirty || locked}>
          Revert
        </button>
        <button type="button" className="btn" onClick={() => void save()} disabled={!dirty || locked}>
          {saving ? "Saving" : "Save storyboard"}
        </button>
        <button type="button" className="btn btn-primary" onClick={() => void renderPreview()} disabled={locked}>
          {rendering ? "Rendering" : dirty ? "Save and render preview" : "Render preview"}
        </button>
      </div>
      <ErrorText error={saveError} />
      <ErrorText error={renderError} />
      {previewJob && (isActive(previewJob) || previewJob.status === "failed") && <JobProgress job={previewJob} />}
      {planJob && (isActive(planJob) || planJob.status === "failed") && <JobProgress job={planJob} />}

      <SolverStrip plan={plan} tolerance={project.brief?.tolerance ?? 0.05} stale={dirty} />

      <TryStyles ctx={ctx} />

      {plan.summary && <p className="plan-summary">{plan.summary}</p>}

      {questions.length > 0 && (
        <QuestionsPanel key={plan.version} questions={questions} busy={planning} error={answerError} onSubmit={answer} />
      )}

      {beats.length === 0 ? (
        <p className="empty">All beats deleted. Revert, or save to clear the storyboard.</p>
      ) : (
        <ol className="beats" aria-label="Beats in play order">
          {beats.map((b, i) => {
            const orig = original.get(b.id);
            const edited =
              !orig ||
              orig.locked !== b.locked ||
              orig.segments.map((s) => s.id).join() !== b.segments.map((s) => s.id).join();
            return (
              <BeatCard
                key={b.id}
                beat={{ ...b, edited }}
                index={i}
                count={beats.length}
                clips={clips}
                dragging={dragFrom === i}
                drop={over && over.index === i && dragFrom !== i ? over.pos : null}
                onMove={(d) => setBeats((list) => moveItem(list, i, d < 0 ? i - 1 : i + 2))}
                onDelete={() => setBeats((list) => list.filter((x) => x.id !== b.id))}
                onToggleLock={() => update(b.id, (x) => ({ ...x, locked: !x.locked }))}
                onRemoveSegment={(sid) => removeSegment(b.id, sid)}
                onRestoreSegment={(sid) => restoreSegment(b.id, sid)}
                onDragStart={onDragStart(i)}
                onDragOver={onDragOver(i)}
                onDrop={onDrop(i)}
                onDragEnd={onDragEnd}
              />
            );
          })}
        </ol>
      )}
    </section>
  );
}
