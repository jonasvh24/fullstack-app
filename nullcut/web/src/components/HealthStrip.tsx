import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Health } from "../api/types";

function Dot({ ok }: { ok: boolean }) {
  return (
    <span className={`dot ${ok ? "dot-ok" : "dot-bad"}`} role="img" aria-label={ok ? "ok" : "unavailable"} />
  );
}

export function HealthStrip() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setHealth(await api.health());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <footer className="health" aria-label="Backend status">
      {error ? (
        <span className="health-item">
          <Dot ok={false} />
          <span className="err-inline">API: {error}</span>
        </span>
      ) : health ? (
        <>
          <span className="health-item">
            <span className="health-key">LLM</span>
            <Dot ok={health.llm.ok} />
            <span className="mono">{health.llm.model || "–"}</span>
            <span className="mono dim">{health.llm.url}</span>
          </span>
          <span className="health-item">
            <span className="health-key">VLM</span>
            <Dot ok={health.vlm.ok} />
            <span className="mono dim">{health.vlm.url}</span>
          </span>
          <span className="health-item">
            <span className="health-key">ASR</span>
            <span className="mono">{health.asr}</span>
          </span>
          <span className="health-item">
            <span className="health-key">Encoder</span>
            <span className="mono">{health.encoder}</span>
          </span>
          <span className="health-item health-version">
            <span className="mono dim">v{health.version}</span>
          </span>
        </>
      ) : (
        <span className="dim">Checking backend</span>
      )}
      <button type="button" className="btn btn-xs" onClick={() => void load()} disabled={busy}>
        Recheck
      </button>
    </footer>
  );
}
