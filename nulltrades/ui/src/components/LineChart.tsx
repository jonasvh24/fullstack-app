import { useRef, useState } from "react";
import { brussels } from "../api";

export interface Point {
  t: string; // ISO time
  v: number;
}

// One series, one axis. A dashed reference line (e.g. target) is optional.
// Hover shows a crosshair and a tooltip with the nearest point.
export default function LineChart({
  points,
  title,
  format,
  reference,
  area = false,
  invert = false,
  height = 220,
}: {
  points: Point[];
  title: string;
  format: (v: number) => string;
  reference?: { v: number; label: string };
  area?: boolean;
  invert?: boolean; // draw values downward from the top (drawdown)
  height?: number;
}) {
  const W = 720;
  const H = height;
  const pad = { l: 64, r: 16, t: 12, b: 26 };
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  if (points.length < 2) {
    return <p className="muted">Not enough data yet for {title.toLowerCase()} (snapshots every 5 minutes).</p>;
  }

  const xs = points.map((p) => new Date(p.t).getTime());
  const vals = points.map((p) => p.v);
  let lo = Math.min(...vals, reference?.v ?? Infinity);
  let hi = Math.max(...vals, reference?.v ?? -Infinity);
  if (invert) lo = 0;
  if (hi === lo) hi = lo + 1;
  const span = hi - lo;
  if (!invert) {
    lo -= span * 0.05;
    hi += span * 0.05;
  }
  const x0 = xs[0];
  const x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
  const sx = (t: number) => pad.l + ((t - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  const sy = (v: number) =>
    invert ? pad.t + ((v - lo) / (hi - lo)) * (H - pad.t - pad.b) : H - pad.b - ((v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  const path = points.map((p, i) => `${i ? "L" : "M"}${sx(xs[i]).toFixed(1)},${sy(p.v).toFixed(1)}`).join("");
  const base = invert ? pad.t : H - pad.b;
  const areaPath = `${path}L${sx(x1).toFixed(1)},${base}L${sx(x0).toFixed(1)},${base}Z`;
  const ticks = [0, 0.5, 1].map((f) => lo + f * (hi - lo));

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const box = ref.current!.getBoundingClientRect();
    const x = ((e.clientX - box.left) / box.width) * W;
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(sx(xs[i]) - x) < Math.abs(sx(xs[best]) - x)) best = i;
    setHover(best);
  };

  const hp = hover !== null ? points[hover] : null;

  return (
    <div className="chart-wrap">
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <svg
        ref={ref}
        className="chart"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`${title}: ${format(vals[0])} to ${format(vals[vals.length - 1])}`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        {ticks.map((v) => (
          <g key={v}>
            <line className="gridline" x1={pad.l} x2={W - pad.r} y1={sy(v)} y2={sy(v)} />
            <text className="axis-label" x={pad.l - 6} y={sy(v) + 4} textAnchor="end">
              {format(v)}
            </text>
          </g>
        ))}
        <text className="axis-label" x={pad.l} y={H - 6}>
          {brussels(points[0].t).slice(0, 16)}
        </text>
        <text className="axis-label" x={W - pad.r} y={H - 6} textAnchor="end">
          {brussels(points[points.length - 1].t).slice(0, 16)}
        </text>
        {reference && (
          <g>
            <line className="ref" x1={pad.l} x2={W - pad.r} y1={sy(reference.v)} y2={sy(reference.v)} />
            <text className="ref-label" x={W - pad.r} y={sy(reference.v) - 5} textAnchor="end">
              {reference.label}
            </text>
          </g>
        )}
        {area && <path className="area" d={areaPath} />}
        <path className="line" d={path} />
        {hp && hover !== null && (
          <g>
            <line className="cross" x1={sx(xs[hover])} x2={sx(xs[hover])} y1={pad.t} y2={H - pad.b} />
            <circle className="dot" cx={sx(xs[hover])} cy={sy(hp.v)} r={4.5} />
          </g>
        )}
      </svg>
      {hp && hover !== null && (
        <div
          className="tooltip"
          style={{
            left: `${(sx(xs[hover]) / W) * 100}%`,
            top: `calc(${(sy(hp.v) / H) * 100}% + 24px)`,
          }}
        >
          <div className="muted">{brussels(hp.t)}</div>
          <div>{format(hp.v)}</div>
        </div>
      )}
    </div>
  );
}
