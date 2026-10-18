import type { Qc } from "../api/types";
import { humanise, num } from "../lib/format";

const STYLE_THRESHOLD = 0.8;

/** style_match values are taken as 0..1; values above 1 are read as percentages. */
function asFraction(v: number): number {
  return v > 1 ? v / 100 : v;
}

function Verdict({ ok, yes, no }: { ok: boolean | null; yes: string; no: string }) {
  if (ok == null) return <span className="tag tag-none">n/a</span>;
  return <span className={`tag ${ok ? "tag-ok" : "tag-bad"}`}>{ok ? yes : no}</span>;
}

export function QcPanel({ qc, title = "QC" }: { qc: Qc; title?: string }) {
  const style = Object.entries(qc.style_match ?? {}).map(([k, v]) => [k, asFraction(v)] as const);
  const flagged = style.filter(([, v]) => v < STYLE_THRESHOLD).length;
  return (
    <section className="qc" aria-label={title}>
      <h3 className="section-title">{title}</h3>
      <dl className="kv">
        <dt>Length</dt>
        <dd>
          <Verdict ok={qc.length_ok} yes="within tolerance" no="out of tolerance" />
        </dd>
        <dt>Loudness</dt>
        <dd className="mono">{num(qc.loudness_lufs, 1, "LUFS")}</dd>
        <dt>True peak</dt>
        <dd className="mono">{num(qc.true_peak_dbtp, 1, "dBTP")}</dd>
        <dt>Cuts</dt>
        <dd>
          <Verdict ok={qc.cuts_clean} yes="clean" no="clicks" />{" "}
          <span className="mono dim">worst {num(qc.worst_cut_db, 1, "dB")}</span>
        </dd>
      </dl>
      <h4 className="sub-title">
        Style match{" "}
        {style.length > 0 && (
          <span className={flagged ? "warn-text" : "dim"}>
            {flagged ? `${flagged} under ${STYLE_THRESHOLD * 100}%` : "all at or above 80%"}
          </span>
        )}
      </h4>
      {style.length === 0 ? (
        <p className="dim">No style measurements.</p>
      ) : (
        <ul className="match-list">
          {style.map(([k, v]) => {
            const low = v < STYLE_THRESHOLD;
            return (
              <li key={k} className={low ? "match low" : "match"}>
                <span className="match-name" title={k}>
                  {humanise(k)}
                </span>
                <span className="meter" aria-hidden="true">
                  <span className="meter-fill" style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%` }} />
                  <span className="meter-mark" style={{ left: `${STYLE_THRESHOLD * 100}%` }} />
                </span>
                <span className="mono match-val">
                  {Math.round(v * 100)}%{low && <span className="sr-only"> (below 80%)</span>}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
