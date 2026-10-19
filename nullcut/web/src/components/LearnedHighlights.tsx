import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Learning } from "../api/types";
import { dateTime, humanise, pct } from "../lib/format";
import { ErrorText } from "./ErrorText";

function weight(v: number | undefined): string {
  if (v == null || !Number.isFinite(v)) return "–";
  return v.toFixed(3);
}

function ResetButton({ onReset, disabled }: { onReset: () => Promise<void>; disabled: boolean }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(t);
  }, [armed]);

  return (
    <button
      type="button"
      className={`btn btn-sm ${armed ? "btn-danger-armed" : "btn-danger"}`}
      disabled={disabled}
      onClick={() => {
        if (!armed) {
          setArmed(true);
          return;
        }
        setArmed(false);
        void onReset();
      }}
      aria-label={armed ? "Confirm reset: remove all examples and learned weights" : "Reset learned highlights"}
    >
      {armed ? "Confirm reset" : "Reset"}
    </button>
  );
}

/** Phase 3: the highlight scorer learned from the creator's keep and delete choices. */
export function LearnedHighlights() {
  const [data, setData] = useState<Learning | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ text: string; ok: boolean } | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.learning());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const refit = async () => {
    setBusy(true);
    setStatus(null);
    setError(null);
    try {
      const fit = await api.refit();
      setStatus(fit && fit.adopted ? { text: "Refit adopted.", ok: true } : { text: "Refit not adopted.", ok: false });
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setBusy(true);
    setStatus(null);
    setError(null);
    try {
      setData(await api.resetLearning());
      setStatus({ text: "Examples and learned weights removed. Hand weights are active.", ok: true });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const fit = data?.last_fit ?? null;
  const coef = fit?.coef ?? null;
  const features = data ? [...new Set([...Object.keys(data.hand_weights), ...Object.keys(coef ?? {})])] : [];

  return (
    <section className="panel" aria-labelledby="learn-title">
      <div className="panel-head">
        <h2 id="learn-title" className="section-title">
          Learned highlights
        </h2>
        {data && (
          <span className={`tag ${data.active === "learned" ? "tag-broll" : "tag-none"}`}>
            {data.active === "learned" ? "learned weights active" : "hand weights active"}
          </span>
        )}
        {data?.new_since_fit && <span className="dim small-text">new examples since the last fit</span>}
        <span className="spacer" />
        <button type="button" className="btn btn-sm" onClick={() => void refit()} disabled={busy || !data}>
          Refit now
        </button>
        <ResetButton onReset={reset} disabled={busy || !data || (data.examples === 0 && !fit)} />
      </div>
      <p className="dim">
        Every sentence you delete or restore teaches it; a refit is adopted only when it ranks your held-out choices
        better than the hand weights.
      </p>
      <ErrorText error={error} />
      {status && (
        <p className={status.ok ? "ok-text" : "warn-text small-text"} role="status">
          {status.text}
        </p>
      )}

      {!data && !error && <p className="dim">Loading</p>}
      {data && (
        <div className="learn-grid">
          <div className="learn-col">
            <dl className="kv">
              <dt>Examples</dt>
              <dd className="mono">{data.examples}</dd>
              <dt>Explicit</dt>
              <dd>
                <span className="mono">{data.explicit}</span>{" "}
                <span className="dim small-text">
                  {data.explicit < data.min_examples
                    ? `${data.min_examples} needed before a refit can be adopted`
                    : `at least ${data.min_examples} needed`}
                </span>
              </dd>
              <dt>Kept</dt>
              <dd className="mono">{data.kept}</dd>
              <dt>Deleted</dt>
              <dd className="mono">{data.deleted}</dd>
              <dt>Projects</dt>
              <dd className="mono">{data.projects}</dd>
              <dt>Next refit</dt>
              <dd className="mono">{data.next_refit ? dateTime(data.next_refit) : "nightly refit off"}</dd>
            </dl>

            <h3 className="sub-title">Last fit</h3>
            {!fit ? (
              <p className="dim">No fit yet.</p>
            ) : (
              <dl className="kv">
                <dt>Fitted</dt>
                <dd className="mono">{dateTime(fit.fitted_at)}</dd>
                <dt>On</dt>
                <dd className="mono">
                  {fit.examples} examples, {fit.explicit} explicit
                </dd>
                <dt>Result</dt>
                <dd>
                  <span className={`tag ${fit.adopted ? "tag-ok" : "tag-warn"}`}>
                    {fit.adopted ? "adopted" : "not adopted"}
                  </span>
                </dd>
                {fit.cv && (
                  <>
                    {fit.cv.learned_auc != null && fit.cv.hand_auc != null && (
                      <>
                        <dt>Held-out AUC</dt>
                        <dd>
                          <span className="mono learn-auc">
                            learned{" "}
                            <span className={fit.cv.learned_auc > fit.cv.hand_auc ? "" : "warn-text"}>
                              {fit.cv.learned_auc.toFixed(2)}
                            </span>{" "}
                            · hand {fit.cv.hand_auc.toFixed(2)}
                          </span>
                        </dd>
                      </>
                    )}
                    <dt>Accuracy</dt>
                    <dd className="mono dim">
                      learned {pct(fit.cv.learned_accuracy)} · hand {pct(fit.cv.hand_accuracy)}
                    </dd>
                    <dt>Log loss</dt>
                    <dd className="mono dim">
                      learned {fit.cv.learned_logloss.toFixed(3)} · hand {fit.cv.hand_logloss.toFixed(3)}
                    </dd>
                  </>
                )}
              </dl>
            )}
            {fit && !fit.adopted && fit.reason && <p className="warn-text small-text">{fit.reason}</p>}
          </div>

          <div className="learn-col">
            <div className="table-wrap">
              <table className="table learn-table">
                <caption className="sr-only">Highlight features: hand weight and learned coefficient</caption>
                <thead>
                  <tr>
                    <th scope="col">Feature</th>
                    <th scope="col" className="num">
                      Hand weight
                    </th>
                    <th scope="col" className="num">
                      Learned coefficient
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {features.map((f) => (
                    <tr key={f}>
                      <td title={f}>{humanise(f)}</td>
                      <td className="num mono">{weight(data.hand_weights[f])}</td>
                      <td className={`num mono ${coef ? "" : "dim"}`}>{weight(coef?.[f])}</td>
                    </tr>
                  ))}
                  {fit?.bias != null && (
                    <tr>
                      <td className="dim">bias</td>
                      <td className="num mono dim">–</td>
                      <td className="num mono">{weight(fit.bias)}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="field-help">
              Learned coefficients are on a logistic scale: compare their sign and order with the hand weights, not
              their size.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
