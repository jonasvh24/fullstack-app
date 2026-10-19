/** Timecode as m:ss.s (minutes are not wrapped into hours). */
export function tc(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "–";
  const neg = s < 0;
  const tenths = Math.round(Math.abs(s) * 10);
  const m = Math.floor(tenths / 600);
  const rem = tenths - m * 600;
  const sec = Math.floor(rem / 10);
  const d = rem % 10;
  return `${neg ? "-" : ""}${m}:${String(sec).padStart(2, "0")}.${d}`;
}

/** Whole-second m:ss, used for chapters and ruler labels. */
export function mss(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "–";
  const t = Math.floor(Math.max(0, s));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

/** m:ss for an editable length field; keeps tenths only when present. */
export function lengthText(s: number | null): string {
  if (s == null) return "";
  const whole = Math.abs(s - Math.round(s)) < 1e-6;
  return whole ? mss(s) : tc(s);
}

/**
 * Parse "m:ss", "m:ss.s" or plain seconds.
 * Returns null for an empty string and NaN for anything unparseable.
 */
export function parseLength(input: string): number | null {
  const v = input.trim();
  if (v === "") return null;
  const colon = /^(\d+):([0-5]?\d(?:\.\d+)?)$/.exec(v);
  if (colon) return Number(colon[1]) * 60 + Number(colon[2]);
  if (/^\d+(?:\.\d+)?$/.test(v)) return Number(v);
  return NaN;
}

export function seconds(s: number | null | undefined, digits = 1): string {
  if (s == null || !Number.isFinite(s)) return "–";
  return `${s.toFixed(digits)} s`;
}

export function fps(v: number): string {
  if (!Number.isFinite(v)) return "–";
  return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

export function resolution(w: number, h: number, rate: number): string {
  return `${w}×${h}@${fps(rate)}`;
}

/** ISO 8601 to "YYYY-MM-DD HH:MM" in local time. */
export function dateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function pct(v: number, digits = 0): string {
  return `${(v * 100).toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 1, unit = ""): string {
  if (v == null || !Number.isFinite(v)) return "–";
  return `${v.toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

/** Compact display of an arbitrary style parameter value. */
export function paramValue(v: unknown): string {
  if (v == null) return "–";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(3)));
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  if (Array.isArray(v) && v.every((x) => typeof x !== "object" || x === null)) return v.map(paramValue).join(", ");
  return JSON.stringify(v);
}

/** Flatten one level of nested objects into dotted keys. */
export function flattenParams(params: Record<string, unknown>): [string, string][] {
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      for (const [k2, v2] of Object.entries(v as Record<string, unknown>)) out.push([`${k}.${k2}`, paramValue(v2)]);
    } else {
      out.push([k, paramValue(v)]);
    }
  }
  return out;
}

export function humanise(key: string): string {
  return key.replace(/[_.]+/g, " ");
}

/** Version-stamped URL so a re-render at the same path is not served from cache. */
export function versioned(url: string, version: number): string {
  return `${url}${url.includes("?") ? "&" : "?"}v=${version}`;
}

/** measured / wanted values from a MatchEntry. */
export function matchValue(v: number | string | null | undefined): string {
  if (v == null) return "–";
  if (typeof v === "number") return paramValue(v);
  return v;
}
