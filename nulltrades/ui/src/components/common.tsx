import { ReactNode, useState } from "react";
import { Mode } from "../api";

// Every page title carries the mode, so a screenshot of paper results can't pass as real.
export function PageTitle({ mode, children }: { mode: Mode; children: ReactNode }) {
  return (
    <h1 style={{ fontSize: 20, margin: "4px 0 16px", display: "flex", gap: 10, alignItems: "center" }}>
      {children}
      <span className={`badge ${mode === "LIVE" ? "live" : "paper"}`}>
        {mode === "LIVE" ? "LIVE" : "PAPER · SIMULATED"}
      </span>
    </h1>
  );
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="error-box" role="alert">
      {error}
    </div>
  );
}

// Runs an async action with a busy flag and captures its error message.
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

export function Pnl({ value, digits = 2 }: { value: number | null | undefined; digits?: number }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  const cls = value > 0 ? "pos" : value < 0 ? "neg" : "";
  return (
    <span className={cls}>
      {value >= 0 ? "+" : "−"}€{Math.abs(value).toFixed(digits)}
    </span>
  );
}
