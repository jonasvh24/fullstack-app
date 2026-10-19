import { useState, type FormEvent } from "react";
import { errorMessage } from "../api/client";
import type { ExtractBody, Job } from "../api/types";
import { isActive, useStandaloneJob } from "../lib/jobs";
import { ErrorText } from "./ErrorText";
import { JobProgress } from "./JobProgress";

/** Name + server folder -> extraction job, with progress polled from /api/jobs/{id}. */
export function ExtractForm({
  id,
  title,
  folderLabel,
  folderPlaceholder,
  submitLabel,
  start,
  onDone,
}: {
  id: string;
  title: string;
  folderLabel: string;
  folderPlaceholder: string;
  submitLabel: string;
  start: (body: ExtractBody) => Promise<Job>;
  onDone: (job: Job) => void;
}) {
  const [name, setName] = useState("");
  const [folder, setFolder] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [job, setJob, pollError] = useStandaloneJob((j) => {
    const made = j.result && typeof j.result === "object" && "name" in j.result ? String(j.result.name) : name.trim();
    setStatus(`Extracted ${made}.`);
    onDone(j);
  });
  const running = isActive(job);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setStatus(null);
    if (!name.trim() || !folder.trim()) {
      setError("Name and folder are both required.");
      return;
    }
    setBusy(true);
    try {
      setJob(await start({ name: name.trim(), folder: folder.trim() }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="extract" onSubmit={submit} aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="section-title">
        {title}
      </h3>
      <div className="form-row">
        <div className="field">
          <label className="field-label" htmlFor={`${id}-name`}>
            Name
          </label>
          <input
            id={`${id}-name`}
            className="input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="off"
          />
        </div>
        <div className="field grow">
          <label className="field-label" htmlFor={`${id}-folder`}>
            {folderLabel}
          </label>
          <input
            id={`${id}-folder`}
            className="input mono"
            type="text"
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder={folderPlaceholder}
            spellCheck={false}
            autoComplete="off"
          />
        </div>
        <button type="submit" className="btn btn-primary" disabled={busy || running}>
          {running ? "Extracting" : submitLabel}
        </button>
      </div>
      <ErrorText error={error} />
      <ErrorText error={pollError && `Job status: ${pollError}`} />
      {job && (isActive(job) || job.status === "failed") && <JobProgress job={job} />}
      {status && (
        <p className="ok-text" role="status">
          {status}
        </p>
      )}
    </form>
  );
}
