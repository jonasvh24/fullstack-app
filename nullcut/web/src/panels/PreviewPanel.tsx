import { useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Clip, TimelineSummary } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { JobProgress } from "../components/JobProgress";
import { QcPanel } from "../components/QcPanel";
import { fps, tc, versioned } from "../lib/format";
import { isActive, latestOfKind } from "../lib/jobs";
import { href } from "../lib/router";
import type { ProjectContext } from "../pages/ProjectPage";
import { TimelineCanvas } from "./TimelineCanvas";

export function PreviewPanel({ ctx }: { ctx: ProjectContext }) {
  const { project, renders, jobs, onJobStarted } = ctx;
  const pid = encodeURIComponent(project.id);
  const preview = renders.find((r) => r.kind === "preview") ?? null;
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const [timeline, setTimeline] = useState<TimelineSummary | null>(null);
  const [tlState, setTlState] = useState<"loading" | "ready" | "missing" | "error">("loading");
  const [tlError, setTlError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const previewJob = latestOfKind(jobs, "preview");
  const rendering = isActive(previewJob);
  const clips = useMemo(() => new Map<string, Clip>(project.clips.map((c) => [c.id, c])), [project.clips]);

  useEffect(() => {
    let cancelled = false;
    setTlState("loading");
    api
      .getTimeline(project.id)
      .then((t) => {
        if (cancelled) return;
        setTimeline(t);
        setTlState(t ? "ready" : "missing");
      })
      .catch((e) => {
        if (cancelled) return;
        setTlError(errorMessage(e));
        setTlState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [project.id, project.timeline_version, preview?.version]);

  const rerender = async () => {
    setStartError(null);
    try {
      onJobStarted(await api.startPreview(project.id));
    } catch (e) {
      setStartError(errorMessage(e));
    }
  };

  return (
    <section className="panel" aria-labelledby="pv-title">
      <div className="panel-head">
        <h2 id="pv-title" className="section-title">
          Preview
        </h2>
        {preview && (
          <span className="mono dim">
            v{preview.version} · {tc(preview.duration_s)}
            {timeline && ` · ${timeline.width}×${timeline.height}@${fps(timeline.fps)}`}
          </span>
        )}
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => void rerender()} disabled={rendering}>
          {rendering ? "Rendering" : "Re-render preview"}
        </button>
        <a className="btn btn-primary" href={href(`/p/${pid}/deliver`)}>
          Deliver
        </a>
      </div>
      <ErrorText error={startError} />
      {previewJob && (isActive(previewJob) || previewJob.status === "failed") && <JobProgress job={previewJob} />}

      {!preview ? (
        <p className="empty">
          No preview render yet. <a href={href(`/p/${pid}/storyboard`)}>Render one from the storyboard.</a>
        </p>
      ) : (
        <div className="player-grid">
          <div className="player">
            <video
              key={`${preview.url}#${preview.version}`}
              ref={setVideo}
              src={versioned(preview.url, preview.version)}
              controls
              preload="metadata"
              playsInline
            />
          </div>
          <QcPanel qc={preview.qc} title={`QC · preview v${preview.version}`} />
        </div>
      )}

      <div className="timeline-block">
        <h3 className="section-title">Timeline</h3>
        {tlState === "ready" && timeline && (
          <>
            <TimelineCanvas timeline={timeline} video={video} clips={clips} />
            <p className="field-help">
              Click to seek. Hover an item for the rules that placed it.{" "}
              <span className="mono">
                v{timeline.version} · {timeline.items.length} items · {timeline.zooms.length} zooms ·{" "}
                {tc(timeline.duration_s)}
              </span>
            </p>
          </>
        )}
        {tlState === "missing" && <p className="empty small">No timeline compiled yet.</p>}
        {tlState === "error" && <ErrorText error={tlError} />}
        {tlState === "loading" && <p className="dim">Loading timeline</p>}
      </div>
    </section>
  );
}
