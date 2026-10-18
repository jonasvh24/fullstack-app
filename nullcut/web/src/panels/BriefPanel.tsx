import { useEffect, useId, useMemo, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Brief, CaptionMode, Energy, MusicMode, Style, Track } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { JobProgress } from "../components/JobProgress";
import { Segmented } from "../components/Segmented";
import { flattenParams, humanise, lengthText, mss, parseLength, tc } from "../lib/format";
import { isActive, latestOfKind, useJobDone } from "../lib/jobs";
import { navigate } from "../lib/router";
import type { ProjectContext } from "../pages/ProjectPage";

const DEFAULT_BRIEF: Brief = {
  text: "",
  target_s: null,
  tolerance: 0.05,
  format: "16:9",
  style: "",
  music: { mode: "none", mood: "", energy: "mid", track_id: null },
  captions: { mode: "off" },
  extras: { zoom: 1, broll: 1, hook_required: false },
};

/** Fill any missing fields so the form is always fully controlled. */
function normalise(b: Partial<Brief> | null | undefined): Brief {
  const src = b ?? {};
  return {
    ...DEFAULT_BRIEF,
    ...src,
    format: "16:9",
    music: { ...DEFAULT_BRIEF.music, ...(src.music ?? {}) },
    captions: { ...DEFAULT_BRIEF.captions, ...(src.captions ?? {}) },
    extras: { ...DEFAULT_BRIEF.extras, ...(src.extras ?? {}) },
  };
}

const MIN_S = 15;
const MAX_S = 1800;

function validateLength(text: string): { value: number | null; error: string | null } {
  const v = parseLength(text);
  if (v == null) return { value: null, error: null };
  if (Number.isNaN(v)) return { value: null, error: "Length must be m:ss or seconds." };
  if (v < MIN_S || v > MAX_S) return { value: null, error: `Length must be between ${mss(MIN_S)} and ${mss(MAX_S)}.` };
  return { value: v, error: null };
}

function StyleParams({ style }: { style: Style }) {
  const rows = flattenParams(style.params ?? {});
  return (
    <div className="style-info">
      <p className="style-desc">{style.description}</p>
      {rows.length > 0 && (
        <div className="param-wrap">
          <table className="param-table">
            <caption className="sr-only">Parameters for {style.name}</caption>
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k}>
                  <th scope="row" title={k}>
                    {humanise(k)}
                  </th>
                  <td className="mono">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Slider({
  label,
  value,
  onChange,
  help,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  help: string;
}) {
  const id = useId();
  return (
    <div className="field slider">
      <span className="field-label">
        <label htmlFor={id}>{label}</label> <span className="mono slider-val">{value.toFixed(1)}×</span>
      </span>
      <input
        id={id}
        aria-describedby={`${id}-help`}
        type="range"
        min={0}
        max={2}
        step={0.1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-valuetext={`${value.toFixed(1)} times style default`}
      />
      <span id={`${id}-help`} className="field-help">
        {help}
      </span>
    </div>
  );
}

export function BriefPanel({ ctx }: { ctx: ProjectContext }) {
  const { project, jobs, onJobStarted, setProject } = ctx;

  // The form is seeded once per project; polling must not overwrite edits in progress.
  const [saved, setSaved] = useState<Brief>(() => normalise(project.brief));
  const [brief, setBrief] = useState<Brief>(saved);
  const [lenText, setLenText] = useState(() => lengthText(saved.target_s));
  const [styles, setStyles] = useState<Style[] | null>(null);
  const [tracks, setTracks] = useState<Track[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.styles(), api.music()])
      .then(([s, t]) => {
        if (cancelled) return;
        setStyles(s);
        setTracks(t);
      })
      .catch((e) => {
        if (!cancelled) setListError(errorMessage(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const len = validateLength(lenText);
  const candidate: Brief = { ...brief, target_s: len.error ? brief.target_s : len.value };
  const dirty =
    lenText.trim() !== lengthText(saved.target_s) || JSON.stringify(candidate) !== JSON.stringify(saved);

  const planJob = latestOfKind(jobs, "plan");
  const planning = isActive(planJob);
  useJobDone(planJob, () => navigate(`/p/${encodeURIComponent(project.id)}/storyboard`));

  const style = useMemo(() => styles?.find((s) => s.id === brief.style) ?? null, [styles, brief.style]);
  const set = <K extends keyof Brief>(k: K, v: Brief[K]) => {
    setError(null);
    setStatus(null);
    setBrief((b) => ({ ...b, [k]: v }));
  };

  const save = async (): Promise<boolean> => {
    setError(null);
    setStatus(null);
    if (len.error) {
      setError(len.error);
      return false;
    }
    if (candidate.music.mode === "track" && !candidate.music.track_id) {
      setError("Choose a track, or switch music to library or none.");
      return false;
    }
    try {
      const next = normalise(await api.putBrief(project.id, candidate));
      setSaved(next);
      setBrief(next);
      setLenText(lengthText(next.target_s));
      setProject((p) => ({ ...p, brief: next }));
      return true;
    } catch (e) {
      setError(errorMessage(e));
      return false;
    }
  };

  const onSave = async () => {
    setBusy(true);
    if (await save()) setStatus("Brief saved.");
    setBusy(false);
  };

  const onPlan = async () => {
    setBusy(true);
    try {
      if (!(await save())) return;
      const job = await api.startPlan(project.id, {});
      onJobStarted(job);
      setStatus(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const music = brief.music;
  const setMusic = (patch: Partial<Brief["music"]>) => set("music", { ...music, ...patch });
  const extras = brief.extras;
  const setExtras = (patch: Partial<Brief["extras"]>) => set("extras", { ...extras, ...patch });
  const track = tracks?.find((t) => t.id === music.track_id) ?? null;

  return (
    <section className="panel" aria-labelledby="brief-title">
      <div className="panel-head">
        <h2 id="brief-title" className="section-title">
          Brief
        </h2>
        {dirty && <span className="tag tag-none">unsaved</span>}
        <span className="spacer" />
        <button type="button" className="btn" onClick={onSave} disabled={busy || planning || !dirty}>
          Save
        </button>
        <button type="button" className="btn btn-primary" onClick={onPlan} disabled={busy || planning}>
          {planning ? "Planning" : "Plan edit"}
        </button>
      </div>
      <ErrorText error={error} />
      {status && (
        <p className="ok-text" role="status">
          {status}
        </p>
      )}
      {planJob && (isActive(planJob) || planJob.status === "failed") && <JobProgress job={planJob} />}
      <ErrorText error={listError && `Styles and music: ${listError}`} />

      <div className="brief-grid">
        <div className="brief-col">
          <label className="field">
            <span className="field-label">Brief</span>
            <textarea
              className="input brief-text"
              value={brief.text}
              onChange={(e) => set("text", e.target.value)}
              rows={8}
              placeholder="Fast travel vlog, open on the cliff jump, end on the sunset."
            />
          </label>

          <div className="form-row">
            <div className="field">
              <label className="field-label" htmlFor="brief-len">
                Length (m:ss)
              </label>
              <span className="inline-input">
                <input
                  id="brief-len"
                  className="input mono len-input"
                  type="text"
                  inputMode="decimal"
                  value={lenText}
                  onChange={(e) => {
                    setError(null);
                    setStatus(null);
                    setLenText(e.target.value);
                  }}
                  placeholder="1:30"
                  aria-invalid={!!len.error}
                  aria-describedby="len-help"
                />
                <span className="mono dim">±{Math.round(brief.tolerance * 100)}%</span>
              </span>
              <span id="len-help" className={len.error ? "field-help err-inline" : "field-help"}>
                {len.error ??
                  (len.value != null
                    ? `${tc(len.value * (1 - brief.tolerance))} to ${tc(len.value * (1 + brief.tolerance))}`
                    : "Blank for no target.")}
              </span>
            </div>
            <Segmented<"16:9" | "9:16">
              legend="Format"
              value="16:9"
              onChange={() => set("format", "16:9")}
              options={[
                { value: "16:9", label: "16:9" },
                { value: "9:16", label: "9:16", disabled: true, hint: "Phase 2" },
              ]}
            />
          </div>

          <fieldset className="field group">
            <legend className="field-label">Extras</legend>
            <Slider
              label="Zoom intensity"
              value={extras.zoom}
              onChange={(v) => setExtras({ zoom: v })}
              help="1.0 = style default"
            />
            <Slider
              label="B-roll density"
              value={extras.broll}
              onChange={(v) => setExtras({ broll: v })}
              help="1.0 = style default"
            />
            <label className="check">
              <input
                type="checkbox"
                checked={extras.hook_required}
                onChange={(e) => setExtras({ hook_required: e.target.checked })}
              />
              <span>Hook required</span>
            </label>
          </fieldset>
        </div>

        <div className="brief-col">
          <div className="field">
            <label className="field-label" htmlFor="brief-style">
              Style
            </label>
            <select id="brief-style" className="input" value={brief.style} onChange={(e) => set("style", e.target.value)}>
              {!style && <option value={brief.style}>{brief.style ? `${brief.style} (unknown)` : "Select a style"}</option>}
              {(styles ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
          {style && <StyleParams style={style} />}

          <fieldset className="field group">
            <legend className="field-label">Music</legend>
            <div className="form-row">
              <Segmented<MusicMode>
                legend="Source"
                value={music.mode}
                onChange={(v) => setMusic({ mode: v })}
                options={[
                  { value: "none", label: "None" },
                  { value: "library", label: "Library" },
                  { value: "track", label: "Track" },
                ]}
              />
              <Segmented<Energy>
                legend="Energy"
                value={music.energy}
                onChange={(v) => setMusic({ energy: v })}
                options={[
                  { value: "low", label: "Low" },
                  { value: "mid", label: "Mid" },
                  { value: "high", label: "High" },
                ]}
              />
            </div>
            <label className="field">
              <span className="field-label">Mood</span>
              <input
                className="input"
                type="text"
                value={music.mood}
                onChange={(e) => setMusic({ mood: e.target.value })}
                placeholder="warm, driving, understated"
                disabled={music.mode === "none"}
              />
            </label>
            {music.mode === "track" && (
              <div className="field">
                <label className="field-label" htmlFor="brief-track">
                  Library track
                </label>
                <select
                  id="brief-track"
                  aria-describedby={track ? "brief-track-help" : undefined}
                  className="input"
                  value={music.track_id ?? ""}
                  onChange={(e) => setMusic({ track_id: e.target.value || null })}
                >
                  <option value="">{tracks && tracks.length === 0 ? "Library is empty" : "Select a track"}</option>
                  {(tracks ?? []).map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name} · {tc(t.duration)} · {Math.round(t.bpm)} bpm
                    </option>
                  ))}
                </select>
                {track && (
                  <span id="brief-track-help" className="field-help mono">
                    energy {track.energy.toFixed(2)} · {track.tags.join(", ") || "no tags"} · {track.licence}
                  </span>
                )}
              </div>
            )}
          </fieldset>

          <Segmented<CaptionMode>
            legend="Captions"
            value={brief.captions.mode}
            onChange={(v) => set("captions", { mode: v })}
            options={[
              { value: "off", label: "Off" },
              { value: "clean", label: "Clean" },
              { value: "word", label: "Word" },
            ]}
          />
        </div>
      </div>
    </section>
  );
}
