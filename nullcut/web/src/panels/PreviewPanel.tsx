import { useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Clip, Render, TimelineSummary } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { JobProgress } from "../components/JobProgress";
import { FormatToggle, useFormatRenders } from "../components/FormatToggle";
import { QcPanel } from "../components/QcPanel";
import { fps, tc, versioned } from "../lib/format";
import { isActive, latestOfKind } from "../lib/jobs";
import { href } from "../lib/router";
import type { ProjectContext } from "../pages/ProjectPage";
import { RefinePanel, refineResult } from "./RefinePanel";
import { TimelineCanvas } from "./TimelineCanvas";

export function PreviewPanel({ ctx }: { ctx: ProjectContext }) {
  const { project, renders, jobs, onJobStarted, refreshRenders } = ctx;
  const pid = encodeURIComponent(project.id);
  const { render: stored, formats, format, setFormat } = useFormatRenders(renders, "preview");
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const [timeline, setTimeline] = useState<TimelineSummary | null>(null);
  const [tlState, setTlState] = useState<"loading" | "ready" | "missing" | "error">("loading");
  const [tlError, setTlError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const previewJob = latestOfKind(jobs, "preview");
  const refineJob = latestOfKind(jobs, "refine");
  const refining = isActive(refineJob);
  const rendering = isActive(previewJob) || refining;

  // A refinement's preview is playable before its job ends (QC follows). Show it at once,
  // and refetch renders so the other format of a "both" brief appears too.
  const live = refining ? refineResult(refineJob)?.render ?? null : null;
  const preview: Render | null =
    live && (!stored || (live.version > stored.version && (live.format ?? "16:9") === (stored.format ?? "16:9")))
      ? live
      : stored;
  const liveKey = live ? `${refineJob?.id}:${live.version}` : null;
  useEffect(() => {
    if (liveKey) void refreshRenders();
  }, [liveKey, refreshRenders]);
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
            v{preview.version} · {preview.format ?? "16:9"} · {tc(preview.duration_s)}
            {timeline && preview.format !== "9:16" && ` · ${timeline.width}×${timeline.height}@${fps(timeline.fps)}`}
          </span>
        )}
        <span className="spacer" />
        <FormatToggle formats={formats} value={format} onChange={setFormat} />
        <button type="button" className="btn" onClick={() => void rerender()} disabled={rendering}>
          {isActive(previewJob) ? "Rendering" : "Re-render preview"}
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
        <div className="player-grid with-side">
          <div className={`player ${preview.format === "9:16" ? "portrait" : ""}`}>
            <video
              key={`${preview.url}#${preview.version}#${preview.format}`}
              ref={setVideo}
              src={versioned(preview.url, preview.version)}
              controls
              preload="metadata"
              playsInline
            />
          </div>
          <div className="player-side">
            <RefinePanel ctx={ctx} refineJob={refineJob} />
            <QcPanel
              qc={preview.qc}
              pending={!!preview.qc_pending}
              title={`QC · preview v${preview.version} · ${preview.format ?? "16:9"}`}
            />
          </div>
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
