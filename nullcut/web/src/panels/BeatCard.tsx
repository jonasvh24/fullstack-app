import type { DragEvent } from "react";
import type { Beat, Clip, Segment } from "../api/types";
import { Thumb } from "../components/Thumb";
import { tc } from "../lib/format";

/** A beat as edited locally: segments may be removed before saving. */
export interface LocalBeat extends Beat {
  removed: Segment[];
  edited: boolean;
}

export type DropPos = "before" | "after";

function SegmentRow({
  seg,
  clip,
  action,
}: {
  seg: Segment;
  clip: Clip | undefined;
  action?: { label: string; aria: string; onClick: () => void; disabled?: boolean };
}) {
  return (
    <li className="seg-row">
      <Thumb src={seg.thumb_url} width={72} />
      <div className="seg-main">
        <div className="seg-meta mono">
          <span className="seg-range">
            {tc(seg.start)}–{tc(seg.end)}
          </span>
          <span className="dim truncate" title={clip?.name ?? seg.clip_id}>
            {clip?.name ?? seg.clip_id}
          </span>
          <span className="score" title={`score ${seg.score.toFixed(2)}`} aria-label={`score ${seg.score.toFixed(2)}`}>
            <span className="score-fill" style={{ width: `${Math.max(0, Math.min(1, seg.score)) * 100}%` }} />
          </span>
        </div>
        <p className="seg-text">{seg.text || <span className="dim">No speech</span>}</p>
      </div>
      {action && (
        <button
          type="button"
          className="btn btn-xs"
          onClick={action.onClick}
          disabled={action.disabled}
          aria-label={action.aria}
        >
          {action.label}
        </button>
      )}
    </li>
  );
}

export interface BeatCardProps {
  beat: LocalBeat;
  index: number;
  count: number;
  clips: Map<string, Clip>;
  drop: DropPos | null;
  dragging: boolean;
  onMove: (delta: -1 | 1) => void;
  onDelete: () => void;
  onToggleLock: () => void;
  onRemoveSegment: (segId: string) => void;
  onRestoreSegment: (segId: string) => void;
  onDragStart: (e: DragEvent) => void;
  onDragOver: (e: DragEvent) => void;
  onDrop: (e: DragEvent) => void;
  onDragEnd: () => void;
}

export function BeatCard(p: BeatCardProps) {
  const { beat, index, count, clips } = p;
  const ratio = beat.target_s ? beat.duration_s / beat.target_s : null;
  const locked = beat.locked;
  const lockHint = locked ? "Unlock to edit" : undefined;

  return (
    <li
      className={`beat role-${beat.role} ${locked ? "is-locked" : ""} ${p.dragging ? "is-dragging" : ""} ${
        p.drop ? `drop-${p.drop}` : ""
      }`}
      draggable
      onDragStart={p.onDragStart}
      onDragOver={p.onDragOver}
      onDrop={p.onDrop}
      onDragEnd={p.onDragEnd}
      aria-label={`Beat ${index + 1} of ${count}: ${beat.role}, ${beat.title}`}
    >
      <div className="beat-head">
        <span className="grip" aria-hidden="true" title="Drag to reorder" />
        <span className="mono dim beat-n">{String(index + 1).padStart(2, "0")}</span>
        <span className={`role role-${beat.role}`}>{beat.role}</span>
        <h3 className="beat-title">{beat.title}</h3>
        {beat.edited && <span className="tag tag-none">edited</span>}
        {locked && <span className="tag tag-lock">locked</span>}
        <span className="spacer" />
        <span className="beat-dur mono" title="Duration after jump cuts vs beat target">
          {tc(beat.duration_s)}
          <span className="dim"> / {beat.target_s != null ? tc(beat.target_s) : "–"}</span>
        </span>
        {ratio != null && (
          <span className="mini-meter" aria-hidden="true">
            <span
              className={`mini-fill ${ratio > 1.05 ? "over" : ratio < 0.95 ? "short" : ""}`}
              style={{ width: `${Math.min(1, ratio) * 100}%` }}
            />
          </span>
        )}
        <span className="beat-actions">
          <button type="button" className="btn btn-xs" onClick={() => p.onMove(-1)} disabled={index === 0} aria-label="Move beat up">
            Up
          </button>
          <button
            type="button"
            className="btn btn-xs"
            onClick={() => p.onMove(1)}
            disabled={index === count - 1}
            aria-label="Move beat down"
          >
            Down
          </button>
          <button type="button" className="btn btn-xs" onClick={p.onToggleLock} aria-pressed={locked}>
            {locked ? "Unlock" : "Lock"}
          </button>
          <button type="button" className="btn btn-xs btn-danger" onClick={p.onDelete} disabled={locked} title={lockHint}>
            Delete
          </button>
        </span>
      </div>

      <div className="beat-sub">
        <p className="beat-intent">{beat.intent}</p>
        <div className="beat-tags">
          <span className="kv-inline">
            <span className="dim">music</span> <span className="mono">{beat.music_energy}</span>
          </span>
          {beat.emphasis.length > 0 && (
            <span className="chips" aria-label="Emphasis words">
              {beat.emphasis.map((w, i) => (
                <span key={`${w}-${i}`} className="chip">
                  {w}
                </span>
              ))}
            </span>
          )}
        </div>
      </div>

      {beat.segments.length === 0 ? (
        <p className="empty small">No segments. Saving keeps the beat empty; the solver may drop it.</p>
      ) : (
        <ol className="seg-list">
          {beat.segments.map((s) => (
            <SegmentRow
              key={s.id}
              seg={s}
              clip={clips.get(s.clip_id)}
              action={{
                label: "Remove",
                aria: `Remove segment ${tc(s.start)} to ${tc(s.end)}`,
                onClick: () => p.onRemoveSegment(s.id),
                disabled: locked,
              }}
            />
          ))}
        </ol>
      )}

      {beat.removed.length > 0 && (
        <details className="seg-fold removed" open>
          <summary>
            <span className="mono">{beat.removed.length}</span> removed (unsaved)
          </summary>
          <ol className="seg-list">
            {beat.removed.map((s) => (
              <SegmentRow
                key={s.id}
                seg={s}
                clip={clips.get(s.clip_id)}
                action={{
                  label: "Restore",
                  aria: `Restore segment ${tc(s.start)} to ${tc(s.end)}`,
                  onClick: () => p.onRestoreSegment(s.id),
                  disabled: locked,
                }}
              />
            ))}
          </ol>
        </details>
      )}

      {beat.dropped.length > 0 && (
        <details className="seg-fold dropped">
          <summary>
            <span className="mono">{beat.dropped.length}</span> cut by length solver
          </summary>
          <ol className="seg-list">
            {beat.dropped.map((s) => (
              <SegmentRow key={s.id} seg={s} clip={clips.get(s.clip_id)} />
            ))}
          </ol>
        </details>
      )}
    </li>
  );
}
