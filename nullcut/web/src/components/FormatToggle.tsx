import { useState } from "react";
import type { Format, Render } from "../api/types";
import { Segmented } from "./Segmented";

/**
 * The renders of one kind at its newest version, and which format is on screen.
 * Renders arrive newest first; formats of the newest version are offered, 16:9 first.
 */
export function useFormatRenders(renders: Render[], kind: Render["kind"]) {
  const ofKind = renders.filter((r) => r.kind === kind);
  const newest = ofKind.reduce<number | null>((v, r) => (v == null || r.version > v ? r.version : v), null);
  const current = ofKind.filter((r) => r.version === newest);
  const formats = (["16:9", "9:16"] as Format[]).filter((f) => current.some((r) => (r.format ?? "16:9") === f));
  const [picked, setPicked] = useState<Format | null>(null);
  const format: Format | null = picked && formats.includes(picked) ? picked : formats[0] ?? null;
  const render = current.find((r) => (r.format ?? "16:9") === format) ?? current[0] ?? null;
  return { render, formats, format, setFormat: setPicked, current };
}

export function FormatToggle({
  formats,
  value,
  onChange,
}: {
  formats: Format[];
  value: Format | null;
  onChange: (f: Format) => void;
}) {
  if (formats.length < 2 || !value) return null;
  return (
    <Segmented<Format>
      legend="Format"
      hideLegend
      value={value}
      onChange={onChange}
      options={formats.map((f) => ({ value: f, label: f }))}
    />
  );
}
