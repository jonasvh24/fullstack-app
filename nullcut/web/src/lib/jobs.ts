import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Job, JobKind, JobStatus } from "../api/types";
import { usePoll } from "./usePoll";

export function isActiveStatus(s: JobStatus): boolean {
  return s === "queued" || s === "running";
}

export function isActive(job: Job | null | undefined): boolean {
  return !!job && isActiveStatus(job.status);
}

/** Newest job of a kind, by `created`. */
export function latestOfKind(jobs: Job[], kind: JobKind): Job | null {
  let best: Job | null = null;
  for (const j of jobs) {
    if (j.kind !== kind) continue;
    if (!best || j.created > best.created) best = j;
  }
  return best;
}

/** Union by id; on conflict keep the more recently updated copy (terminal wins a tie). */
export function mergeJobs(...lists: Job[][]): Job[] {
  const byId = new Map<string, Job>();
  for (const list of lists) {
    for (const j of list) {
      const prev = byId.get(j.id);
      if (!prev) byId.set(j.id, j);
      else if (j.updated > prev.updated) byId.set(j.id, j);
      else if (j.updated === prev.updated && isActive(prev) && !isActive(j)) byId.set(j.id, j);
    }
  }
  return [...byId.values()];
}

/**
 * Fire `onDone` once when the observed job goes from queued/running to done.
 * Jobs that were already finished when first seen do not fire.
 */
export function useJobDone(job: Job | null, onDone: (job: Job) => void): void {
  const prev = useRef<{ id: string; status: JobStatus } | null>(null);
  const cb = useRef(onDone);
  useEffect(() => {
    cb.current = onDone;
  });
  const id = job?.id;
  const status = job?.status;
  useEffect(() => {
    const p = prev.current;
    if (job && p && p.id === job.id && isActiveStatus(p.status) && job.status === "done") cb.current(job);
    prev.current = job ? { id: job.id, status: job.status } : null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, status]);
}

/**
 * Track a job that is not tied to a project page (style and thumbnail extraction):
 * polls GET /api/jobs/{id} every second while it is queued or running.
 */
export function useStandaloneJob(onDone?: (job: Job) => void): [Job | null, (job: Job) => void, string | null] {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  usePoll(
    async () => {
      if (!job) return;
      try {
        setJob(await api.getJob(job.id));
        setError(null);
      } catch (e) {
        setError(errorMessage(e));
      }
    },
    isActive(job),
    1000,
  );
  useJobDone(job, (j) => onDone?.(j));
  return [job, setJob, error];
}
