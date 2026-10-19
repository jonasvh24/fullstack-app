import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Job, Style, TryRender } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { JobProgress } from "../components/JobProgress";
import { MATCH_THRESHOLD, MatchList, asFraction, flaggedCount } from "../components/MatchList";
import { tc } from "../lib/format";
import { isActive, latestOfKind, useJobDone } from "../lib/jobs";
import { href } from "../lib/router";
import type { ProjectContext } from "../pages/ProjectPage";

const MIN = 2;
const MAX = 4;
// The contract does not name the try job's kind; "try" is assumed for picking up a run after a reload.
const TRY_KIND = "try";

function TryCard({
  r,
  current,
  busy,
  onUse,
}: {
  r: TryRender;
  current: boolean;
  busy: boolean;
  onUse: () => void;
}) {
  const overall = asFraction(r.overall);
  const flagged = flaggedCount(undefined, r.match);
  return (
    <li className="try-card">
      <video src={r.url} controls preload="metadata" playsInline aria-label={`Trial render in ${r.style_name}`} />
      <div className="try-head">
        <span className="try-name">{r.style_name}</span>
        <span className="mono dim">{tc(r.duration_s)}</span>
        <span className="spacer" />
        <span className={`try-overall mono ${overall < MATCH_THRESHOLD ? "warn-text" : ""}`} title="Overall style match">
          {Math.round(overall * 100)}%
        </span>
      </div>
      <p className={flagged ? "warn-text small-text" : "dim small-text"}>
        {flagged ? `${flagged} parameter${flagged === 1 ? "" : "s"} under 80%` : "All parameters at or above 80%"}
      </p>
      <MatchList detail={r.match} />
      <div className="try-foot">
        {current ? (
          <span className="tag tag-ok">brief style</span>
        ) : (
          <button type="button" className="btn btn-sm" onClick={onUse} disabled={busy}>
            Use this style
          </button>
        )}
      </div>
    </li>
  );
}

export function TryStyles({ ctx }: { ctx: ProjectContext }) {
  const { project, jobs, onJobStarted, setProject } = ctx;
  const [open, setOpen] = useState(false);
  const [styles, setStyles] = useState<Style[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [seconds, setSeconds] = useState("60");
  const [results, setResults] = useState<TryRender[]>([]);
  const [startedId, setStartedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [useError, setUseError] = useState<string | null>(null);
  const [useStatus, setUseStatus] = useState<string | null>(null);
  const [using, setUsing] = useState(false);

  const loadResults = async () => {
    try {
      setResults(await api.getTry(project.id));
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  useEffect(() => {
    let cancelled = false;
    api
      .getTry(project.id)
      .then((r) => {
        if (!cancelled) setResults(r);
      })
      .catch((e) => {
        if (!cancelled) setError(errorMessage(e));
      });
    api
      .styles()
      .then((s) => {
        if (!cancelled) setStyles(s);
      })
      .catch((e) => {
        if (!cancelled) setError(errorMessage(e));
      });
    return () => {
      cancelled = true;
    };
  }, [project.id]);

  const job: Job | null = (startedId ? jobs.find((j) => j.id === startedId) : null) ?? latestOfKind(jobs, TRY_KIND);
  const running = isActive(job);
  useJobDone(job, (j) => {
    const rs = j.result && Array.isArray(j.result.renders) ? (j.result.renders as TryRender[]) : null;
    if (rs) setResults(rs);
    else void loadResults();
  });

  const secs = Number(seconds);
  const secsOk = seconds.trim() !== "" && Number.isFinite(secs) && secs > 0;
  const canStart = picked.length >= MIN && picked.length <= MAX && secsOk && !running;

  const toggle = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length >= MAX ? p : [...p, id]));

  const start = async () => {
    setError(null);
    try {
      const j = await api.startTry(project.id, { styles: picked, seconds: secs });
      setStartedId(j.id);
      onJobStarted(j);
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  const useStyle = async (r: TryRender) => {
    setUsing(true);
    setUseError(null);
    setUseStatus(null);
    try {
      const brief = await api.putBrief(project.id, { ...project.brief, style: r.style });
      setProject((p) => ({ ...p, brief }));
      setUseStatus(`Brief style set to ${r.style_name}. Re-plan from the Brief to apply it.`);
    } catch (e) {
      setUseError(errorMessage(e));
    } finally {
      setUsing(false);
    }
  };

  return (
    <details className="try" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>
        <span className="section-title">Try styles</span>
        <span className="dim small-text">
          {results.length > 0 ? `${results.length} trial renders` : "Render a short trial in 2–4 styles side by side"}
        </span>
        {running && <span className="tag tag-running">running</span>}
      </summary>
      <div className="try-body">
        <fieldset className="field">
          <legend className="field-label">
            Styles <span className="mono dim">{picked.length}/{MAX}</span>
          </legend>
          <div className="try-picks">
            {(styles ?? []).map((s) => {
              const on = picked.includes(s.id);
              return (
                <label key={s.id} className={`pick ${on ? "is-on" : ""}`}>
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={!on && picked.length >= MAX}
                    onChange={() => toggle(s.id)}
                  />
                  <span>{s.name}</span>
                  {s.id === project.brief?.style && <span className="dim small-text">brief</span>}
                </label>
              );
            })}
            {!styles && <span className="dim">Loading styles</span>}
          </div>
        </fieldset>
        <div className="form-row">
          <div className="field">
            <label className="field-label" htmlFor="try-seconds">
              Seconds
            </label>
            <input
              id="try-seconds"
              className="input mono secs-input"
              type="number"
              min={1}
              step={1}
              value={seconds}
              onChange={(e) => setSeconds(e.target.value)}
              aria-invalid={!secsOk}
            />
          </div>
          <button type="button" className="btn btn-primary" onClick={() => void start()} disabled={!canStart}>
            {running ? "Rendering trials" : "Render trials"}
          </button>
          {picked.length < MIN && <span className="field-help">Pick at least {MIN} styles.</span>}
        </div>
        <ErrorText error={error} />
        {job && (isActive(job) || job.status === "failed") && <JobProgress job={job} />}
        <ErrorText error={useError} />
        {useStatus && (
          <p className="ok-text" role="status">
            {useStatus} <a href={href(`/p/${encodeURIComponent(project.id)}/brief`)}>Go to Brief</a>
          </p>
        )}
        {results.length > 0 && (
          <ul className="try-grid" aria-label="Trial renders">
            {results.map((r) => (
              <TryCard
                key={`${r.style}:${r.url}`}
                r={r}
                current={r.style === project.brief?.style}
                busy={using}
                onUse={() => void useStyle(r)}
              />
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}
