import type { MatchEntry } from "../api/types";
import { humanise, matchValue } from "../lib/format";

export const MATCH_THRESHOLD = 0.8;

/** Scores may arrive as 0..1 or as percentages; normalise to 0..1. */
export function asFraction(v: number): number {
  return v > 1 ? v / 100 : v;
}

export function pctText(v: number): string {
  return `${Math.round(asFraction(v) * 100)}%`;
}

/**
 * Per-parameter style scores. With `detail`, each row also shows measured against wanted,
 * and the note for anything under 80%.
 */
export function MatchList({
  scores,
  detail,
}: {
  scores?: Record<string, number>;
  detail?: Record<string, MatchEntry>;
}) {
  const rows: { key: string; score: number; entry?: MatchEntry }[] = detail
    ? Object.entries(detail).map(([key, entry]) => ({ key, score: asFraction(entry.score), entry }))
    : Object.entries(scores ?? {}).map(([key, v]) => ({ key, score: asFraction(v) }));

  if (rows.length === 0) return <p className="dim">No style measurements.</p>;
  return (
    <ul className="match-list">
      {rows.map(({ key, score, entry }) => {
        const low = score < MATCH_THRESHOLD;
        return (
          <li key={key} className={low ? "match low" : "match"}>
            <span className="match-name" title={key}>
              {humanise(key)}
            </span>
            <span className="meter" aria-hidden="true">
              <span className="meter-fill" style={{ width: `${Math.max(0, Math.min(1, score)) * 100}%` }} />
              <span className="meter-mark" style={{ left: `${MATCH_THRESHOLD * 100}%` }} />
            </span>
            <span className="mono match-val">
              {Math.round(score * 100)}%{low && <span className="sr-only"> (below 80%)</span>}
            </span>
            {entry && (
              <span className="match-detail mono">
                <span className="dim">measured</span> {matchValue(entry.measured)} <span className="dim">· wanted</span>{" "}
                {matchValue(entry.wanted)}
              </span>
            )}
            {entry && low && entry.note && <span className="match-note">{entry.note}</span>}
          </li>
        );
      })}
    </ul>
  );
}

export function flaggedCount(scores?: Record<string, number>, detail?: Record<string, MatchEntry>): number {
  const values = detail ? Object.values(detail).map((e) => e.score) : Object.values(scores ?? {});
  return values.filter((v) => asFraction(v) < MATCH_THRESHOLD).length;
}
