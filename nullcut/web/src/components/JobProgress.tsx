import type { Job } from "../api/types";
import { isActive } from "../lib/jobs";

const LABEL: Record<Job["kind"], string> = {
  ingest: "Ingest",
  plan: "Plan",
  preview: "Preview render",
  master: "Master render",
  music_import: "Music import",
};

/** Progress bar and current step for a job. Failed jobs show their error in red monospace. */
export function JobProgress({ job, showDone = false }: { job: Job | null; showDone?: boolean }) {
  if (!job) return null;
  if (job.status === "done" && !showDone) return null;
  const active = isActive(job);
  const p = Math.max(0, Math.min(1, job.progress || 0));
  return (
    <div className={`job job-${job.status}`} aria-live="polite">
      <div className="job-head">
        <span className="job-kind">{LABEL[job.kind] ?? job.kind}</span>
        <span className={`tag tag-${job.status}`}>{job.status}</span>
        <span className="mono job-pct">{active || job.status === "done" ? `${Math.round(p * 100)}%` : ""}</span>
        <span className="job-msg" title={job.message}>
          {job.message}
        </span>
      </div>
      {job.status !== "failed" && (
        <div
          className="bar"
          role="progressbar"
          aria-label={`${LABEL[job.kind] ?? job.kind} progress`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(p * 100)}
        >
          <span className="bar-fill" style={{ width: `${p * 100}%` }} />
        </div>
      )}
      {job.status === "failed" && <pre className="job-error">{job.error || "Job failed without an error message."}</pre>}
    </div>
  );
}
