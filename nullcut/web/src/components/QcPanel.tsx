import type { Qc } from "../api/types";
import { num } from "../lib/format";
import { MATCH_THRESHOLD, MatchList, asFraction, flaggedCount } from "./MatchList";

function Verdict({ ok, yes, no }: { ok: boolean | null; yes: string; no: string }) {
  if (ok == null) return <span className="tag tag-none">n/a</span>;
  return <span className={`tag ${ok ? "tag-ok" : "tag-bad"}`}>{ok ? yes : no}</span>;
}

/**
 * Gate checks and style match for one render. While `pending` (Phase 3: a refinement's preview
 * plays before its style is measured), the style score and detail are replaced by a note.
 */
export function QcPanel({ qc, title = "QC", pending = false }: { qc: Qc; title?: string; pending?: boolean }) {
  const detail = qc.style_detail && Object.keys(qc.style_detail).length > 0 ? qc.style_detail : undefined;
  const count = detail ? Object.keys(detail).length : Object.keys(qc.style_match ?? {}).length;
  const flagged = flaggedCount(qc.style_match, detail);
  const overall = qc.style_overall != null ? asFraction(qc.style_overall) : null;
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
      <div className="style-head">
        <h4 className="sub-title">Style match</h4>
        {pending && <span className="tag tag-running">measuring</span>}
        {!pending && overall != null && (
          <span className={`style-overall mono ${overall < MATCH_THRESHOLD ? "warn-text" : ""}`} title="Mean of the per-parameter scores">
            {Math.round(overall * 100)}%
          </span>
        )}
        {!pending && count > 0 && (
          <span className={flagged ? "warn-text small-text" : "dim small-text"}>
            {flagged ? `${flagged} under ${MATCH_THRESHOLD * 100}%` : "all at or above 80%"}
          </span>
        )}
      </div>
      {pending ? (
        <p className="dim" aria-live="polite">
          Measuring the style…
        </p>
      ) : (
        <MatchList scores={qc.style_match} detail={detail} />
      )}
    </section>
  );
}
