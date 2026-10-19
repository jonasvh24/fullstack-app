import type { CompareRow } from "../api/types";
import { paramValue } from "../lib/format";

/** Four categorical slots, validated for the dark surface (CVD and contrast). Shapes back up colour. */
export const SERIES = [
  { colour: "#5b7fff", shape: "circle" },
  { colour: "#d95926", shape: "square" },
  { colour: "#199e70", shape: "triangle" },
  { colour: "#c98500", shape: "diamond" },
] as const;

export interface ChartSeries {
  id: string;
  name: string;
  slot: number; // fixed per style while it stays selected, so colours never repaint
}

export function Glyph({ slot, size = 10 }: { slot: number; size?: number }) {
  const s = SERIES[slot % SERIES.length];
  return (
    <svg width={size} height={size} viewBox="-6 -6 12 12" aria-hidden="true" className="glyph">
      <Shape shape={s.shape} colour={s.colour} />
    </svg>
  );
}

function Shape({ shape, colour }: { shape: string; colour: string }) {
  const ring = { stroke: "var(--bg-2)", strokeWidth: 1.5 };
  switch (shape) {
    case "square":
      return <rect x={-4.5} y={-4.5} width={9} height={9} fill={colour} {...ring} />;
    case "triangle":
      return <path d="M0,-5.5 L5.5,4.5 L-5.5,4.5 Z" fill={colour} {...ring} />;
    case "diamond":
      return <path d="M0,-6 L6,0 L0,6 L-6,0 Z" fill={colour} {...ring} />;
    default:
      return <circle r={5} fill={colour} {...ring} />;
  }
}

function withUnit(v: number | null, unit: string): string {
  if (v == null) return "–";
  return unit ? `${paramValue(v)} ${unit}` : paramValue(v);
}

/** One row per parameter: every style's marker on the same min..max axis. */
export function CompareChart({ rows, series }: { rows: CompareRow[]; series: ChartSeries[] }) {
  const lane = 9;
  const h = 10 + series.length * lane;
  return (
    <div className="compare">
      <ul className="legend" aria-label="Legend">
        {series.map((s) => (
          <li key={s.id}>
            <Glyph slot={s.slot} />
            <span>{s.name}</span>
          </li>
        ))}
      </ul>
      <div className="compare-rows" role="table" aria-label="Style parameters compared">
        <div className="compare-row compare-head" role="row">
          <span role="columnheader">Parameter</span>
          <span role="columnheader">Range</span>
          <span role="columnheader">Values</span>
        </div>
        {rows.map((row) => {
          const span = row.max - row.min;
          const pos = (v: number) => (span > 0 ? Math.max(0, Math.min(1, (v - row.min) / span)) : 0.5);
          return (
            <div className="compare-row" role="row" key={row.key}>
              <span className="compare-label" role="rowheader" title={row.key}>
                {row.label}
                {row.unit && <span className="dim mono"> {row.unit}</span>}
              </span>
              <span className="compare-axis" role="cell">
                <svg width="100%" height={h} className="axis-svg" aria-hidden="true">
                  <line x1="0" x2="100%" y1={h / 2} y2={h / 2} className="axis-line" />
                  <line x1="0" x2="0" y1={h / 2 - 4} y2={h / 2 + 4} className="axis-line" />
                  <line x1="100%" x2="100%" y1={h / 2 - 4} y2={h / 2 + 4} className="axis-line" />
                  {series.map((s, i) => {
                    const v = row.values[s.id];
                    if (v == null) return null;
                    const y = 5 + i * lane + lane / 2;
                    return (
                      <svg key={s.id} x={`${pos(v) * 100}%`} y={y} overflow="visible">
                        <title>{`${s.name}: ${withUnit(v, row.unit)}`}</title>
                        <Shape shape={SERIES[s.slot % SERIES.length].shape} colour={SERIES[s.slot % SERIES.length].colour} />
                      </svg>
                    );
                  })}
                </svg>
                <span className="axis-ends mono">
                  <span>{withUnit(row.min, row.unit)}</span>
                  <span>{withUnit(row.max, row.unit)}</span>
                </span>
              </span>
              <span className="compare-values" role="cell">
                {series.map((s) => (
                  <span key={s.id} className="compare-value">
                    <Glyph slot={s.slot} size={9} />
                    <span className="mono">{withUnit(row.values[s.id] ?? null, row.unit)}</span>
                    <span className="sr-only"> {s.name}</span>
                  </span>
                ))}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
