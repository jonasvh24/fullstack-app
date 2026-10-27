import { Fragment, useEffect, useState } from "react";
import { api, brussels, Decision, Mode, num } from "../api";
import { ErrorBox, PageTitle } from "../components/common";

export function DecisionDetail({ d, showOutput = false }: { d: Decision; showOutput?: boolean }) {
  return (
    <div>
      <dl className="kv">
        <dt>Time</dt>
        <dd>
          {brussels(d.ts)} <span className="muted">(candle {brussels(d.candle_ts)})</span>
        </dd>
        <dt>Model</dt>
        <dd className="mono">
          {d.model} · prompt {d.prompt_version}
          {d.cache_hit ? " · cached" : ""}
        </dd>
        <dt>Validation</dt>
        <dd className={d.valid ? "pos" : "neg"}>{d.valid ? "valid" : `rejected: ${d.validation_errors.join("; ")}`}</dd>
        <dt>Snapshot</dt>
        <dd className="mono">{d.snapshot_hash.slice(0, 16)}…</dd>
      </dl>
      {d.verdicts.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Action</th>
                <th>Pair</th>
                <th>Risk verdict</th>
                <th className="num">Asked €</th>
                <th className="num">Final €</th>
                <th className="num">Conf.</th>
                <th>Rationale</th>
              </tr>
            </thead>
            <tbody>
              {d.verdicts.map((v) => (
                <tr key={v.action_index}>
                  <td>{v.action_index}</td>
                  <td>{v.action}</td>
                  <td>{v.pair ?? "—"}</td>
                  <td>
                    <span className={`verdict ${v.verdict}`}>{v.verdict}</span>
                    {v.reasons.length > 0 && <div className="hint">{v.reasons.join("; ")}</div>}
                  </td>
                  <td className="num">{num(v.size_requested, 4)}</td>
                  <td className="num">{num(v.size_final, 4)}</td>
                  <td className="num">{v.confidence ?? "—"}</td>
                  <td style={{ maxWidth: 320 }}>{v.rationale}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {d.orders.length > 0 && (
        <>
          <h3>Orders</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Side</th>
                  <th>Type</th>
                  <th>Purpose</th>
                  <th className="num">Amount</th>
                  <th className="num">Limit</th>
                  <th>Status</th>
                  <th className="num">Filled</th>
                  <th className="num">Avg</th>
                  <th className="num">Fee €</th>
                </tr>
              </thead>
              <tbody>
                {d.orders.map((o) => (
                  <tr key={o.id}>
                    <td>{o.pair}</td>
                    <td>{o.side}</td>
                    <td>{o.type}</td>
                    <td>{o.purpose}</td>
                    <td className="num">{num(o.amount)}</td>
                    <td className="num">{num(o.price)}</td>
                    <td>
                      {o.status}
                      {o.error && <div className="hint">{o.error}</div>}
                    </td>
                    <td className="num">{num(o.filled)}</td>
                    <td className="num">{num(o.average)}</td>
                    <td className="num">{o.fee.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {showOutput && d.model_output && (
        <>
          <h3>Model output</h3>
          <pre>{d.model_output}</pre>
        </>
      )}
    </div>
  );
}

export default function Decisions({ mode }: { mode: Mode }) {
  const [rows, setRows] = useState<Decision[]>([]);
  const [onlyRejections, setOnlyRejections] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .get<{ decisions: Decision[] }>(`/api/decisions?rejections=${onlyRejections ? 1 : 0}&limit=200`)
        .then((r) => alive && (setRows(r.decisions), setError(null)))
        .catch((e) => alive && setError(String(e.message ?? e)));
    load();
    const t = window.setInterval(load, 10000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [onlyRejections, mode]);

  return (
    <>
      <PageTitle mode={mode}>Decisions</PageTitle>
      <div className="btns" style={{ marginBottom: 12, alignItems: "center" }}>
        <label className="check">
          <input type="checkbox" checked={onlyRejections} onChange={(e) => setOnlyRejections(e.target.checked)} />
          Rejections only
        </label>
        <span className="spacer" style={{ flex: 1 }} />
        <a className="button" href="/api/export/decisions.csv">
          Export CSV ({mode === "PAPER" ? "PAPER · SIMULATED" : "LIVE"})
        </a>
      </div>
      <ErrorBox error={error} />
      <section className="card">
        {rows.length === 0 ? (
          <p className="muted">No decisions{onlyRejections ? " with rejections" : ""}.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time (Brussels)</th>
                  <th>Pairs</th>
                  <th>Actions</th>
                  <th>Validation</th>
                  <th>Risk verdicts</th>
                  <th>Orders</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((d) => (
                  <Fragment key={d.id}>
                    <tr className="clickable" onClick={() => setOpen(open === d.id ? null : d.id)}>
                      <td>{brussels(d.ts)}</td>
                      <td>{d.pairs.join(", ") || "—"}</td>
                      <td>{d.actions.map((a) => String(a.action)).join(", ") || (d.valid ? "hold" : "—")}</td>
                      <td className={d.valid ? "pos" : "neg"}>{d.valid ? "valid" : "rejected"}</td>
                      <td>
                        {d.verdicts.map((v) => (
                          <span key={v.action_index} className={`verdict ${v.verdict}`} style={{ marginRight: 8 }}>
                            {v.verdict}
                          </span>
                        ))}
                      </td>
                      <td>{d.orders.map((o) => `${o.side} ${o.status}`).join(", ") || "—"}</td>
                    </tr>
                    {open === d.id && (
                      <tr>
                        <td colSpan={6}>
                          <DecisionDetail d={d} showOutput />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
