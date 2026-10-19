import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, errorMessage } from "../api/client";
import type { Job, RefineKind, RefineResponse, RefineResult, RefineStep } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { isActive, useJobDone } from "../lib/jobs";
import type { ProjectContext } from "../pages/ProjectPage";

const SUGGESTIONS = ["less zoom", "music quieter", "punchier first 15 seconds", "warmer", "no B-roll", "undo"];

const KIND_TAG: Record<RefineKind, string> = {
  tweak: "tag-none",
  structural: "tag-broll",
  undo: "tag-lock",
};

/** The refine job's `{step, render}` once the preview is playable (while running) or final. */
export function refineResult(job: Job | null | undefined): RefineResult | null {
  const r: unknown = job?.result;
  if (!r || typeof r !== "object") return null;
  const v = r as Partial<RefineResult>;
  return v.step && v.render ? (v as RefineResult) : null;
}

type Tone = "info" | "warn" | "error";

/** A reply that is not a stored step: not understood, refused, or the request failed. */
interface Note {
  key: number;
  after: number | null; // id of the last step when it happened, to keep chat order
  instruction: string | null;
  text: string;
  tone: Tone;
}

function statsLine(step: RefineStep): string | null {
  const parts: string[] = [];
  const r = step.render;
  if (r) {
    parts.push(r.encoded === 0 ? `all ${r.items} items reused` : `re-encoded ${r.encoded} of ${r.items} items`);
    parts.push(r.audio_remixed ? "audio remixed" : "audio reused");
  }
  const t = step.timing;
  if (t) {
    parts.push(`preview in ${t.to_preview_s.toFixed(1)} s`);
    if (t.qc_s != null) parts.push(`QC in ${t.qc_s.toFixed(1)} s`);
  }
  return parts.length ? parts.join(" · ") : null;
}

function StepTurn({ step }: { step: RefineStep }) {
  const stats = statsLine(step);
  // A one-change tweak's summary is often its label; do not repeat it.
  const changes = (step.changes ?? []).filter((c) => c.label !== step.summary);
  const unmapped = step.unmapped ?? [];
  return (
    <li className={`rf-turn ${step.undone ? "is-undone" : ""}`}>
      <p className="rf-msg rf-user">{step.instruction}</p>
      <div className="rf-msg rf-sys">
        <div className="rf-meta">
          <span className={`tag ${KIND_TAG[step.kind] ?? "tag-none"}`}>{step.kind}</span>
          <span className="mono dim">by {step.by}</span>
          {step.undone && <span className="tag tag-warn">undone</span>}
          <span className="spacer" />
          <span className="mono dim" title={`Step ${step.id}, plan v${step.plan_version}`}>
            #{step.id}
          </span>
        </div>
        {step.summary && <p className="rf-summary">{step.summary}</p>}
        {changes.length > 0 && (
          <ul className="rf-changes">
            {changes.map((c, i) => (
              <li key={`${c.param}:${i}`} className="mono" title={c.param}>
                {c.label}
              </li>
            ))}
          </ul>
        )}
        {unmapped.length > 0 && <p className="rf-unmapped">Not understood: {unmapped.join("; ")}</p>}
        {stats && <p className="rf-stats mono">{stats}</p>}
      </div>
    </li>
  );
}

function NoteTurn({ note }: { note: Note }) {
  return (
    <li className="rf-turn">
      {note.instruction && <p className="rf-msg rf-user">{note.instruction}</p>}
      <p className={`rf-msg rf-sys rf-${note.tone}`} role={note.tone === "info" ? undefined : "alert"}>
        {note.text}
      </p>
    </li>
  );
}

function LiveTurn({ job, playable }: { job: Job; playable: boolean }) {
  const p = Math.max(0, Math.min(1, job.progress || 0));
  return (
    <li className="rf-turn" aria-live="polite">
      <div className="rf-msg rf-sys rf-live">
        <div className="rf-meta">
          <span className="tag tag-running">{playable ? "measuring" : "rendering"}</span>
          <span className="mono dim">{Math.round(p * 100)}%</span>
        </div>
        <p className="mono dim rf-progress" title={job.message}>
          {job.message || "queued"}
        </p>
        <div className="bar" role="progressbar" aria-label="Refinement progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(p * 100)}>
          <span className="bar-fill" style={{ width: `${p * 100}%` }} />
        </div>
        {playable && <p className="small-text">Preview updated. QC is measuring the style.</p>}
      </div>
    </li>
  );
}

/**
 * Chat refinement on the Preview step: one instruction in, an updated preview out.
 * The job is watched by the project page; this panel only reads it from `refineJob`.
 */
export function RefinePanel({ ctx, refineJob }: { ctx: ProjectContext; refineJob: Job | null }) {
  const { project, onJobStarted, reload } = ctx;
  const [steps, setSteps] = useState<RefineStep[] | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const listRef = useRef<HTMLOListElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const noteKey = useRef(0);
  const stepsRef = useRef(steps);
  stepsRef.current = steps;

  const refining = isActive(refineJob);
  const busy = refining || sending;
  const live = refineResult(refineJob);

  const loadHistory = useCallback(async () => {
    try {
      setSteps(await api.refinements(project.id));
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, [project.id]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // Final QC and the step's timing land when the job ends; undo also flips earlier steps.
  useJobDone(refineJob, () => void loadHistory());

  const addNote = (instruction: string | null, text: string, tone: Tone) => {
    const list = stepsRef.current ?? [];
    const after = list.length ? list[list.length - 1].id : null;
    setNotes((prev) => [...prev, { key: ++noteKey.current, after, instruction, text, tone }]);
  };

  const accept = (res: RefineResponse, instruction: string) => {
    if (res.job) onJobStarted(res.job);
    if (res.step) {
      const step = res.step;
      setSteps((prev) => [...(prev ?? []).filter((s) => s.id !== step.id), step]);
      // The brief (overrides) and the plan version change as soon as the step is applied.
      void loadHistory();
      void reload();
    } else {
      addNote(instruction, res.message || "That was not understood. Try one of the suggestions.", "info");
    }
  };

  const submit = async (instruction: string, undo = false) => {
    const text = instruction.trim();
    if (!text || busy) return;
    setSending(true);
    let keepDraft = false;
    try {
      accept(undo ? await api.undo(project.id) : await api.refine(project.id, text), text);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        addNote(text, "Still rendering the previous change.", "warn");
        keepDraft = true;
      } else if (e instanceof ApiError && e.status === 400) {
        addNote(text, e.message, "warn");
      } else {
        addNote(text, errorMessage(e), "error");
        keepDraft = true;
      }
    } finally {
      setSending(false);
      if (!keepDraft && !undo) setDraft("");
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit(draft);
  };

  // Steps oldest first. While the job runs (or until history is refetched) its copy of the
  // step carries the render stats; afterwards the stored step wins, since undo can flip it.
  const shown: RefineStep[] = (steps ?? []).map((s) =>
    live && s.id === live.step.id && (refining || !s.render) ? live.step : s,
  );
  if (live && refining && !shown.some((s) => s.id === live.step.id)) shown.push(live.step);
  const notesAfter = (id: number | null) => notes.filter((n) => n.after === id);
  const failed = refineJob && refineJob.status === "failed" ? refineJob : null;

  const scrollKey = `${shown.length}:${notes.length}:${refineJob?.message ?? ""}:${refineJob?.status ?? ""}:${!!live}`;
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [scrollKey]);

  // Focus returns to the input once a refinement has finished, so the next one can be typed.
  const wasBusy = useRef(false);
  useEffect(() => {
    if (wasBusy.current && !busy) inputRef.current?.focus();
    wasBusy.current = busy;
  }, [busy]);

  const empty = steps != null && shown.length === 0 && notes.length === 0 && !refining && !failed;

  return (
    <section className="refine" aria-labelledby="rf-title">
      <div className="panel-head">
        <h3 id="rf-title" className="section-title">
          Refine
        </h3>
        {shown.length > 0 && (
          <span className="mono dim">
            {shown.length} {shown.length === 1 ? "step" : "steps"}
          </span>
        )}
        <span className="spacer" />
        <button
          type="button"
          className="btn btn-xs"
          onClick={() => void submit("undo", true)}
          disabled={busy || shown.length === 0}
          title="Undo the last refinement"
        >
          Undo
        </button>
      </div>
      <ErrorText error={loadError && `History: ${loadError}`} />

      <ol className="rf-log" ref={listRef} aria-label="Refinement history">
        {steps == null && !loadError && <li className="dim small-text">Loading history</li>}
        {empty && (
          <li className="rf-empty">
            Say what to change and the preview re-renders. Only the affected items are encoded again.
          </li>
        )}
        {notesAfter(null).map((n) => (
          <NoteTurn key={`n${n.key}`} note={n} />
        ))}
        {shown.map((s) => (
          <StepFragment key={`s${s.id}`} step={s} notes={notesAfter(s.id)} />
        ))}
        {refineJob && refining && <LiveTurn job={refineJob} playable={!!live} />}
        {failed && (
          <li className="rf-turn">
            <p className="rf-msg rf-sys rf-error" role="alert">
              Render failed: {failed.error || "no error message"}
            </p>
          </li>
        )}
      </ol>

      <div className="rf-chips" role="group" aria-label="Suggestions">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            className="btn btn-xs rf-chip"
            disabled={busy}
            onClick={() => {
              setDraft(s);
              void submit(s);
            }}
          >
            {s}
          </button>
        ))}
      </div>
      <form className="rf-form" onSubmit={onSubmit}>
        <label className="sr-only" htmlFor="rf-input">
          What to change
        </label>
        <input
          id="rf-input"
          ref={inputRef}
          className="input"
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={refining ? "Rendering the last change" : "e.g. drop the part about pricing"}
          disabled={busy}
          autoComplete="off"
        />
        <button type="submit" className="btn btn-primary" disabled={busy || !draft.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}

function StepFragment({ step, notes }: { step: RefineStep; notes: Note[] }) {
  return (
    <>
      <StepTurn step={step} />
      {notes.map((n) => (
        <NoteTurn key={`n${n.key}`} note={n} />
      ))}
    </>
  );
}
