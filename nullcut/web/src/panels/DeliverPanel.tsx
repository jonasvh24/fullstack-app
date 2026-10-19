import { useEffect, useRef, useState, type RefObject } from "react";
import { api, errorMessage } from "../api/client";
import type { ExportCheck, Package, Thumbnail } from "../api/types";
import { FormatToggle, useFormatRenders } from "../components/FormatToggle";
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

function Check({ ok, label }: { ok: boolean; label: string }) {
  return <span className={`tag ${ok ? "tag-ok" : "tag-bad"}`}>{label}</span>;
}

// The contract's ExportCheck does not carry the FCPXML version; the exporter writes 1.8.
const FCPXML_VERSION = "1.8";

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** One line for the FCPXML structural check and read-back, then errors and warnings. */
function ExportCheckLine({ check }: { check: ExportCheck }) {
  const parts: string[] = [];
  if (check.spine_clips != null) parts.push(plural(check.spine_clips, "cut"));
  if (check.connected_clips != null) parts.push(plural(check.connected_clips, "connected clip"));
  const rt = check.roundtrip;
  if (rt?.read) parts.push(rt.durations_match ? "read back identically" : "read back with differences");
  else if (rt) parts.push("not read back");
  return (
    <div className="export-check">
      <p className={check.ok ? "ok-text" : "err-inline"}>
        FCPXML {FCPXML_VERSION} {check.ok ? "checked" : "check failed"}
        {parts.length > 0 && `: ${parts.join(", ")}`}
      </p>
      {check.errors.length > 0 && (
        <ul className="check-list err-inline mono">
          {check.errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}
      {check.warnings.length > 0 && (
        <ul className="check-list dim mono">
          {check.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ResolveHelp({ hasCube }: { hasCube: boolean }) {
  return (
    <section className="pkg-block" aria-labelledby="resolve-h">
      <h3 id="resolve-h" className="section-title">
        Open in Resolve
      </h3>
      <ol className="steps-list">
        <li>
          File › Import › Timeline, then choose the <span className="mono">.fcpxml</span> (or the{" "}
          <span className="mono">.otio</span> on Resolve 18.5 and later).
        </li>
        <li>
          Import the <span className="mono">.srt</span> onto a subtitle track.
        </li>
        {hasCube && (
          <li>
            Apply the <span className="mono">.cube</span> on a colour node.
          </li>
        )}
      </ol>
      <p className="field-help">Keep the source media where it is; the timeline links to it by path.</p>
    </section>
  );
}

function ThumbnailsBlock({ thumbs }: { thumbs: Thumbnail[] }) {
  return (
    <section className="pkg-block thumbs-block" aria-labelledby="thumbs-h">
      <h3 id="thumbs-h" className="section-title">
        Thumbnails
      </h3>
      <ol className="thumbs">
        {thumbs.map((t) => (
          <li key={`${t.rank}:${t.url}`} className={`thumb-card ${t.checks.passed ? "" : "failed"}`}>
            <img src={t.url} alt={`Thumbnail ${t.rank}: ${t.text}`} loading="lazy" />
            <div className="thumb-head">
              <span className="mono dim">#{t.rank}</span>
              <span className="thumb-text">{t.text || <span className="dim">No text</span>}</span>
              <span className="spacer" />
              <span className={`tag ${t.checks.passed ? "tag-ok" : "tag-bad"}`}>{t.checks.passed ? "passed" : "failed"}</span>
            </div>
            <dl className="kv thumb-kv">
              <dt>Layout</dt>
              <dd className="mono">{t.layout}</dd>
              <dt>Score</dt>
              <dd className="mono">{t.score.toFixed(2)}</dd>
              <dt>Frame</dt>
              <dd className="mono">{tc(t.frame_t)}</dd>
              <dt>Contrast</dt>
              <dd>
                <span className="mono">{t.checks.contrast_ratio.toFixed(1)}:1</span>{" "}
                <Check ok={t.checks.contrast_ok} label={t.checks.contrast_ok ? "pass" : "fail"} />
              </dd>
              <dt>Face</dt>
              <dd>
                <span className="mono">{Math.round(t.checks.face_px)} px</span>{" "}
                <Check ok={t.checks.face_ok} label={t.checks.face_ok ? "pass" : "fail"} />
              </dd>
              <dt>Corner</dt>
              <dd>
                <Check ok={t.checks.corner_clear} label={t.checks.corner_clear ? "clear" : "blocked"} />
              </dd>
            </dl>
          </li>
        ))}
      </ol>
    </section>
  );
}

function PackageView({ pkg, mp4, mp4Vertical }: { pkg: Package; mp4: string | null; mp4Vertical: string | null }) {
  const descRef = useRef<HTMLTextAreaElement>(null);
  const thumbs = [...(pkg.thumbnails ?? [])].sort((a, b) => a.rank - b.rank);
  const files: [string, string | null][] = [
    ["MP4", mp4],
    ["MP4 9:16", mp4Vertical],
    ["SRT", pkg.files.srt],
    ["VTT", pkg.files.vtt],
    ["OTIO", pkg.files.otio],
    ["FCPXML", pkg.files.fcpxml],
    ["Grade LUT (.cube)", pkg.files.cube ?? null],
    ...thumbs.map((t): [string, string] => [`PNG ${t.rank}`, t.url]),
  ];
  return (
    <div className="pkg-grid">
      {thumbs.length > 0 && <ThumbnailsBlock thumbs={thumbs} />}
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
        {pkg.export_checks?.fcpxml && <ExportCheckLine check={pkg.export_checks.fcpxml} />}
      </section>

      {(pkg.files.fcpxml || pkg.files.otio) && <ResolveHelp hasCube={!!pkg.files.cube} />}
    </div>
  );
}

export function DeliverPanel({ ctx }: { ctx: ProjectContext }) {
  const { project, renders, jobs, onJobStarted } = ctx;
  const { render: master, formats, format, setFormat, current } = useFormatRenders(renders, "master");
  const [pkg, setPkg] = useState<Package | null>(null);
  const [pkgError, setPkgError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const masterJob = latestOfKind(jobs, "master");
  // MP4 is the 16:9 master; "MP4 9:16" is files.mp4_vertical. A 9:16-only brief has no 16:9 master,
  // so its render stands in for the vertical file if the package does not name one.
  const landscape = current.find((r) => (r.format ?? "16:9") === "16:9") ?? null;
  const portrait = current.find((r) => r.format === "9:16") ?? null;
  const mp4 = landscape?.url ?? null;
  const mp4Vertical = pkg?.files.mp4_vertical ?? (landscape ? null : portrait?.url ?? null);
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
            master v{master.version} · {master.format ?? "16:9"} · {tc(master.duration_s)}
          </span>
        )}
        <span className="spacer" />
        <FormatToggle formats={formats} value={format} onChange={setFormat} />
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
          <div className={`player ${master.format === "9:16" ? "portrait" : ""}`}>
            <video
              key={`${master.url}#${master.version}#${master.format}`}
              src={versioned(master.url, master.version)}
              controls
              preload="metadata"
              playsInline
            />
          </div>
          <QcPanel qc={master.qc} pending={!!master.qc_pending} title={`QC · master v${master.version} · ${master.format ?? "16:9"}`} />
        </div>
      )}

      <ErrorText error={pkgError && `Package: ${pkgError}`} />
      {pkg && <PackageView pkg={pkg} mp4={mp4} mp4Vertical={mp4Vertical} />}
    </section>
  );
}
