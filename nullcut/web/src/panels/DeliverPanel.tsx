import { useEffect, useRef, useState, type RefObject } from "react";
import { api, errorMessage } from "../api/client";
import type { Package } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { JobProgress } from "../components/JobProgress";
import { QcPanel } from "../components/QcPanel";
import { mss, tc, versioned } from "../lib/format";
import { isActive, latestOfKind } from "../lib/jobs";
import type { ProjectContext } from "../pages/ProjectPage";

function fileName(url: string): string {
  const clean = url.split("?")[0];
  return clean.slice(clean.lastIndexOf("/") + 1) || url;
}

function CopyButton({ text, target }: { text: string; target: RefObject<HTMLTextAreaElement> }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = window.setTimeout(() => setState("idle"), 1600);
    return () => window.clearTimeout(t);
  }, [state]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      // Clipboard API needs a secure context; localhost qualifies, but fall back to a selection.
      const el = target.current;
      if (el) {
        el.focus();
        el.select();
        setState(document.execCommand("copy") ? "copied" : "failed");
      } else setState("failed");
    }
  };

  return (
    <button type="button" className="btn btn-xs" onClick={() => void copy()}>
      {state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : "Copy"}
      <span className="sr-only" aria-live="polite">
        {state === "copied" ? "Description copied" : ""}
      </span>
    </button>
  );
}

function PackageView({ pkg, mp4 }: { pkg: Package; mp4: string | null }) {
  const descRef = useRef<HTMLTextAreaElement>(null);
  const files: [string, string | null][] = [
    ["MP4", mp4],
    ["SRT", pkg.files.srt],
    ["VTT", pkg.files.vtt],
    ["OTIO", pkg.files.otio],
    ["FCPXML", pkg.files.fcpxml],
  ];
  return (
    <div className="pkg-grid">
      <section className="pkg-block" aria-labelledby="titles-h">
        <h3 id="titles-h" className="section-title">
          Titles
        </h3>
        <ol className="titles">
          {pkg.titles.map((t, i) => (
            <li key={i} className={`title-row ${t.verified ? "" : "unverified"}`}>
              <span className="mono dim title-rank">{i + 1}</span>
              <div className="title-main">
                <div className="title-text">
                  {t.text}{" "}
                  <span className={`tag ${t.verified ? "tag-ok" : "tag-warn"}`}>
                    {t.verified ? "verified" : "unverified"}
                  </span>
                </div>
                <p className="title-why">{t.rationale}</p>
              </div>
            </li>
          ))}
        </ol>
        {pkg.titles.some((t) => !t.verified) && (
          <p className="field-help">Unverified titles make claims the transcript does not support. Check before use.</p>
        )}
      </section>

      <section className="pkg-block" aria-labelledby="desc-h">
        <div className="panel-head">
          <h3 id="desc-h" className="section-title">
            Description
          </h3>
          <span className="spacer" />
          <CopyButton text={pkg.description} target={descRef} />
        </div>
        <label className="sr-only" htmlFor="pkg-desc">
          Description
        </label>
        <textarea id="pkg-desc" ref={descRef} className="input desc" readOnly value={pkg.description} rows={9} />
      </section>

      <section className="pkg-block" aria-labelledby="ch-h">
        <h3 id="ch-h" className="section-title">
          Chapters
        </h3>
        {pkg.chapters.length === 0 ? (
          <p className="dim">No chapters.</p>
        ) : (
          <ol className="chapters">
            {pkg.chapters.map((c, i) => (
              <li key={i}>
                <span className="mono">{mss(c.t)}</span>
                <span>{c.title}</span>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="pkg-block" aria-labelledby="dl-h">
        <h3 id="dl-h" className="section-title">
          Downloads
        </h3>
        <ul className="downloads">
          {files
            .filter((f): f is [string, string] => !!f[1])
            .map(([label, url]) => (
              <li key={label}>
                <a className="btn btn-sm" href={url} download={fileName(url)}>
                  {label}
                </a>
                <span className="mono dim truncate">{fileName(url)}</span>
              </li>
            ))}
        </ul>
      </section>
    </div>
  );
}

export function DeliverPanel({ ctx }: { ctx: ProjectContext }) {
  const { project, renders, jobs, onJobStarted } = ctx;
  const master = renders.find((r) => r.kind === "master") ?? null;
  const [pkg, setPkg] = useState<Package | null>(null);
  const [pkgError, setPkgError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const masterJob = latestOfKind(jobs, "master");
  const rendering = isActive(masterJob);

  useEffect(() => {
    let cancelled = false;
    api
      .getPackage(project.id)
      .then((p) => {
        if (!cancelled) {
          setPkg(p);
          setPkgError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setPkgError(errorMessage(e));
      });
    return () => {
      cancelled = true;
    };
  }, [project.id, master?.version]);

  const start = async () => {
    setStartError(null);
    try {
      onJobStarted(await api.startMaster(project.id));
    } catch (e) {
      setStartError(errorMessage(e));
    }
  };

  return (
    <section className="panel" aria-labelledby="dv-title">
      <div className="panel-head">
        <h2 id="dv-title" className="section-title">
          Deliver
        </h2>
        {master && (
          <span className="mono dim">
            master v{master.version} · {tc(master.duration_s)}
          </span>
        )}
        <span className="spacer" />
        <button type="button" className="btn btn-primary" onClick={() => void start()} disabled={rendering}>
          {rendering ? "Rendering" : master ? "Re-render master" : "Render master"}
        </button>
      </div>
      <ErrorText error={startError} />
      {masterJob && (isActive(masterJob) || masterJob.status === "failed") && <JobProgress job={masterJob} />}

      {!master ? (
        <p className="empty">No master yet. The master renders at full quality and builds the publishing package.</p>
      ) : (
        <div className="player-grid">
          <div className="player">
            <video
              key={`${master.url}#${master.version}`}
              src={versioned(master.url, master.version)}
              controls
              preload="metadata"
              playsInline
            />
          </div>
          <QcPanel qc={master.qc} title={`QC · master v${master.version}`} />
        </div>
      )}

      <ErrorText error={pkgError && `Package: ${pkgError}`} />
      {pkg && <PackageView pkg={pkg} mp4={master?.url ?? null} />}
    </section>
  );
}
