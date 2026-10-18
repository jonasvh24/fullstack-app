import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import type { BeatRole, Clip, TimelineSummary } from "../api/types";
import { mss, tc } from "../lib/format";

// Geometry, in CSS pixels.
const GUTTER = 44;
const PAD_R = 8;
const ROWS = {
  ruler: { y: 0, h: 16 },
  beats: { y: 18, h: 22 },
  v1: { y: 44, h: 24 },
  v2: { y: 72, h: 20 },
  zoom: { y: 96, h: 12 },
  music: { y: 112, h: 10 },
} as const;
const HEIGHT = 126;
type RowName = keyof typeof ROWS;

const LABELS: [RowName, string][] = [
  ["beats", "BEAT"],
  ["v1", "V1"],
  ["v2", "V2"],
  ["zoom", "ZOOM"],
  ["music", "MUS"],
];

// Canvas colours mirror the CSS tokens in styles.css.
const C = {
  bg: "#0e1115",
  row: "#12161b",
  line: "#232830",
  text: "#d7dce3",
  dim: "#8b94a1",
  mute: "#5d6672",
  accent: "#0F41F3",
  accentHi: "#5b7fff",
  v1: "#1d2633",
  v1Edge: "#34445a",
  playhead: "#e9edf2",
};

const ROLE_ALPHA: Record<BeatRole, number> = { hook: 0.55, setup: 0.22, body: 0.12, payoff: 0.4, cta: 0.3 };

interface Hit {
  key: string;
  title: string;
  range: [number, number];
  lines: string[];
}

function niceStep(duration: number, width: number): number {
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for (const s of steps) if ((s / duration) * width >= 64) return s;
  return 600;
}

function textFit(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, maxW: number) {
  if (maxW < 12) return;
  let t = text;
  if (ctx.measureText(t).width > maxW) {
    while (t.length > 1 && ctx.measureText(t + "…").width > maxW) t = t.slice(0, -1);
    t += "…";
  }
  ctx.fillText(t, x, y);
}

export function TimelineCanvas({
  timeline,
  video,
  clips,
}: {
  timeline: TimelineSummary;
  video: HTMLVideoElement | null;
  clips: Map<string, Clip>;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(800);
  const [hover, setHover] = useState<{ hit: Hit; x: number; y: number } | null>(null);
  const hoverKey = hover?.hit.key ?? null;

  const duration = Math.max(0.001, timeline.duration_s);
  const span = Math.max(1, width - GUTTER - PAD_R);
  const xOf = useCallback((t: number) => GUTTER + (t / duration) * span, [duration, span]);
  const tOf = useCallback((x: number) => Math.max(0, Math.min(duration, ((x - GUTTER) / span) * duration)), [duration, span]);

  // Track container width.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width);
      if (w > 0) setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(width * dpr);
    const ph = Math.round(HEIGHT * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, HEIGHT);
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, width, HEIGHT);
    ctx.textBaseline = "middle";

    // Row backgrounds and gutter labels.
    ctx.font = '500 9px ui-monospace, "DM Mono", monospace';
    for (const [row, label] of LABELS) {
      const r = ROWS[row];
      ctx.fillStyle = C.row;
      ctx.fillRect(GUTTER, r.y, span, r.h);
      ctx.fillStyle = C.mute;
      ctx.fillText(label, 6, r.y + r.h / 2);
    }

    // Ruler.
    const step = niceStep(duration, span);
    ctx.strokeStyle = C.line;
    ctx.lineWidth = 1;
    ctx.fillStyle = C.dim;
    ctx.font = '10px ui-monospace, "DM Mono", monospace';
    for (let t = 0; t <= duration + 1e-6; t += step) {
      const x = Math.round(xOf(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, ROWS.ruler.y + 10);
      ctx.lineTo(x, HEIGHT);
      ctx.stroke();
      if (x < width - 30) ctx.fillText(step < 1 ? tc(t) : mss(t), x + 3, ROWS.ruler.y + 7);
    }

    const box = (row: RowName, a: number, b: number) => {
      const r = ROWS[row];
      const x0 = xOf(a);
      const x1 = xOf(b);
      return { x: x0, y: r.y, w: Math.max(1, x1 - x0), h: r.h };
    };

    // Beat bands.
    ctx.font = '500 10px "DM Sans", system-ui, sans-serif';
    timeline.beats.forEach((bt, i) => {
      const r = box("beats", bt.start_s, bt.end_s);
      ctx.globalAlpha = ROLE_ALPHA[bt.role] ?? 0.2;
      ctx.fillStyle = C.accent;
      ctx.fillRect(r.x, r.y, r.w, r.h);
      ctx.globalAlpha = 1;
      if (hoverKey === `beat:${i}`) {
        ctx.strokeStyle = C.accentHi;
        ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1);
      }
      ctx.fillStyle = C.bg;
      ctx.fillRect(Math.round(r.x), r.y, 1, r.h);
      ctx.fillStyle = C.text;
      textFit(ctx, `${bt.role.toUpperCase()}  ${bt.title}`, r.x + 5, r.y + r.h / 2, r.w - 8);
    });

    // Video items.
    ctx.font = '10px "DM Sans", system-ui, sans-serif';
    timeline.items.forEach((it, i) => {
      const row: RowName = it.track === "V2" ? "v2" : "v1";
      const r = box(row, it.start_s, it.end_s);
      const hot = hoverKey === `item:${i}`;
      if (row === "v1") {
        ctx.fillStyle = C.v1;
        ctx.fillRect(r.x, r.y + 1, r.w, r.h - 2);
        ctx.strokeStyle = hot ? C.accentHi : C.v1Edge;
      } else {
        ctx.globalAlpha = 0.45;
        ctx.fillStyle = C.accent;
        ctx.fillRect(r.x, r.y + 1, r.w, r.h - 2);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = hot ? C.accentHi : C.accent;
      }
      ctx.lineWidth = hot ? 1.5 : 1;
      ctx.strokeRect(r.x + 0.5, r.y + 1.5, Math.max(0, r.w - 1), r.h - 3);
      ctx.lineWidth = 1;
      ctx.fillStyle = C.dim;
      textFit(ctx, clips.get(it.clip_id)?.name ?? it.clip_id, r.x + 4, r.y + r.h / 2, r.w - 7);
    });

    // Zooms.
    ctx.font = '9px ui-monospace, "DM Mono", monospace';
    timeline.zooms.forEach((z, i) => {
      const r = box("zoom", z.start_s, z.end_s);
      const hot = hoverKey === `zoom:${i}`;
      ctx.fillStyle = hot ? C.accentHi : C.dim;
      ctx.globalAlpha = hot ? 0.9 : 0.35;
      ctx.fillRect(r.x, r.y + 2, r.w, r.h - 4);
      ctx.globalAlpha = 1;
      ctx.fillStyle = C.text;
      ctx.beginPath();
      ctx.moveTo(r.x, r.y + 1);
      ctx.lineTo(r.x + 4, r.y + r.h / 2);
      ctx.lineTo(r.x, r.y + r.h - 1);
      ctx.fill();
      textFit(ctx, `${z.scale.toFixed(2)}×`, r.x + 6, r.y + r.h / 2, r.w - 8);
    });

    // Music.
    if (timeline.music) {
      const m = timeline.music;
      const r = box("music", m.start_s, m.end_s);
      ctx.fillStyle = hoverKey === "music" ? C.accentHi : "#3a4656";
      ctx.fillRect(r.x, r.y + 2, r.w, r.h - 4);
    }

    // Playhead.
    const t = video ? video.currentTime : 0;
    const px = Math.round(xOf(Math.min(t, duration))) + 0.5;
    ctx.strokeStyle = C.playhead;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, ROWS.beats.y - 2);
    ctx.lineTo(px, HEIGHT);
    ctx.stroke();
    ctx.fillStyle = C.accentHi;
    ctx.beginPath();
    ctx.moveTo(px - 5, ROWS.beats.y - 8);
    ctx.lineTo(px + 5, ROWS.beats.y - 8);
    ctx.lineTo(px, ROWS.beats.y - 2);
    ctx.fill();

    // Current time readout in the gutter corner.
    ctx.font = '10px ui-monospace, "DM Mono", monospace';
    ctx.fillStyle = C.text;
    ctx.fillText(tc(t), 4, ROWS.ruler.y + 7);
  }, [width, span, duration, timeline, hoverKey, video, xOf, clips]);

  // Redraw on any input change.
  useEffect(() => {
    draw();
  }, [draw]);

  // Keep the playhead in sync with the video: animate while playing, redraw on discrete events.
  const drawRef = useRef(draw);
  useEffect(() => {
    drawRef.current = draw;
  }, [draw]);
  useEffect(() => {
    if (!video) return;
    let raf = 0;
    const loop = () => {
      drawRef.current();
      raf = requestAnimationFrame(loop);
    };
    const start = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(loop);
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      drawRef.current();
    };
    const once = () => drawRef.current();
    video.addEventListener("play", start);
    video.addEventListener("pause", stop);
    video.addEventListener("ended", stop);
    video.addEventListener("seeked", once);
    video.addEventListener("timeupdate", once);
    video.addEventListener("loadedmetadata", once);
    if (!video.paused) start();
    return () => {
      cancelAnimationFrame(raf);
      video.removeEventListener("play", start);
      video.removeEventListener("pause", stop);
      video.removeEventListener("ended", stop);
      video.removeEventListener("seeked", once);
      video.removeEventListener("timeupdate", once);
      video.removeEventListener("loadedmetadata", once);
    };
  }, [video]);

  // Redraw when the display's pixel ratio changes (browser zoom, moving between screens).
  useEffect(() => {
    const mq = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
    const on = () => drawRef.current();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [width]);

  const hitTest = (x: number, y: number): Hit | null => {
    if (x < GUTTER) return null;
    const t = tOf(x);
    const inRow = (row: RowName) => y >= ROWS[row].y && y < ROWS[row].y + ROWS[row].h;
    const within = (a: number, b: number) => t >= a && t <= b;
    if (inRow("v1") || inRow("v2")) {
      const track = inRow("v1") ? "V1" : "V2";
      const i = timeline.items.findIndex((it) => it.track === track && within(it.start_s, it.end_s));
      if (i < 0) return null;
      const it = timeline.items[i];
      return {
        key: `item:${i}`,
        title: `${it.track} · ${clips.get(it.clip_id)?.name ?? it.clip_id}`,
        range: [it.start_s, it.end_s],
        lines: it.rules.length ? it.rules : ["no rules recorded"],
      };
    }
    if (inRow("beats")) {
      const i = timeline.beats.findIndex((b) => within(b.start_s, b.end_s));
      if (i < 0) return null;
      const b = timeline.beats[i];
      return { key: `beat:${i}`, title: `${b.role} · ${b.title}`, range: [b.start_s, b.end_s], lines: [] };
    }
    if (inRow("zoom")) {
      const i = timeline.zooms.findIndex((z) => within(z.start_s, z.end_s));
      if (i < 0) return null;
      const z = timeline.zooms[i];
      return { key: `zoom:${i}`, title: `Zoom ${z.scale.toFixed(2)}×`, range: [z.start_s, z.end_s], lines: [z.rule] };
    }
    if (inRow("music") && timeline.music && within(timeline.music.start_s, timeline.music.end_s)) {
      const m = timeline.music;
      return { key: "music", title: `Music · ${m.track}`, range: [m.start_s, m.end_s], lines: [] };
    }
    return null;
  };

  const local = (e: MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  };

  const onMove = (e: MouseEvent<HTMLCanvasElement>) => {
    const { x, y } = local(e);
    const hit = hitTest(x, y);
    setHover(hit ? { hit, x, y } : null);
  };

  const seek = (t: number) => {
    if (!video) return;
    video.currentTime = Math.max(0, Math.min(t, Number.isFinite(video.duration) ? video.duration : t));
    drawRef.current();
  };

  const onClick = (e: MouseEvent<HTMLCanvasElement>) => {
    const { x } = local(e);
    if (x >= GUTTER) seek(tOf(x));
  };

  const onKey = (e: KeyboardEvent<HTMLCanvasElement>) => {
    if (!video) return;
    const big = e.shiftKey ? 5 : 1;
    if (e.key === "ArrowLeft") seek(video.currentTime - big);
    else if (e.key === "ArrowRight") seek(video.currentTime + big);
    else if (e.key === "Home") seek(0);
    else if (e.key === "End") seek(duration);
    else if (e.key === " " || e.key === "k") {
      if (video.paused) void video.play();
      else video.pause();
    } else return;
    e.preventDefault();
  };

  const tipLeft = hover ? Math.min(Math.max(hover.x + 12, 0), Math.max(0, width - 280)) : 0;

  return (
    <div className="timeline" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        style={{ width: `${width}px`, height: `${HEIGHT}px` }}
        tabIndex={0}
        role="slider"
        aria-label="Timeline. Left and right arrows seek one second, with Shift five; Space plays or pauses."
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        aria-valuenow={Math.round(video?.currentTime ?? 0)}
        aria-valuetext={tc(video?.currentTime ?? 0)}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        onClick={onClick}
        onKeyDown={onKey}
      />
      {hover && (
        <div className="tl-tip" style={{ left: tipLeft, top: hover.y + 14 }} role="tooltip">
          <div className="tl-tip-title">{hover.hit.title}</div>
          <div className="mono dim">
            {tc(hover.hit.range[0])}–{tc(hover.hit.range[1])} ({(hover.hit.range[1] - hover.hit.range[0]).toFixed(1)} s)
          </div>
          {hover.hit.lines.length > 0 && (
            <ul className="tl-rules">
              {hover.hit.lines.map((l, i) => (
                <li key={i} className="mono">
                  {l}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
