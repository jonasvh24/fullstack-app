import { api, brussels, eur, num, Status } from "../api";
import { ErrorBox, PageTitle, Pnl, useAction } from "../components/common";
import { DecisionDetail } from "./Decisions";

export default function Dashboard({ status, refresh }: { status: Status; refresh: () => void }) {
  const { busy, error, run } = useAction();
  const s = status;
  const r = s.run;
  const latched = s.state === "KILLED" || s.state === "TARGET_REACHED" || s.state === "HALTED";

  const act = (path: string) => run(async () => {
    await api.post(path);
    refresh();
  });

  const kill = () => {
    const what = s.positions.length ? " Open positions will be market-sold." : "";
    if (window.confirm(`Kill the engine now? Open orders are cancelled.${what}`)) act("/api/engine/kill");
  };

  return (
    <>
      <PageTitle mode={s.mode}>Dashboard</PageTitle>
      <ul className="warnings">
        {s.warnings.map((w) => (
          <li key={w}>{w}</li>
        ))}
        {s.restart_required && <li>Settings changed in LIVE: restart the app before trading.</li>}
      </ul>

      <div className="grid">
        <section className="card">
          <h2>Engine</h2>
          <dl className="kv">
            <dt>State</dt>
            <dd>
              <span className={`badge state ${s.state}`}>{s.state.replace("_", " ")}</span>
            </dd>
            {s.state_reason && (
              <>
                <dt>Reason</dt>
                <dd>{s.state_reason}</dd>
              </>
            )}
            <dt>Model</dt>
            <dd className="mono">{s.model} · prompt {s.prompt_version}</dd>
            <dt>Timeframe</dt>
            <dd>{s.timeframe}</dd>
            <dt>Reconciled</dt>
            <dd>
              {s.last_reconcile
                ? `${brussels(s.last_reconcile.ts)} · ${s.last_reconcile.ok ? "OK" : "MISMATCH"}`
                : "not yet"}
            </dd>
            {s.last_error && (
              <>
                <dt>Last error</dt>
                <dd className="neg">{s.last_error}</dd>
              </>
            )}
          </dl>
          <h3>Controls</h3>
          <div className="btns">
            <button className="primary" disabled={busy || !r || s.state !== "STOPPED"} onClick={() => act("/api/engine/start")}>
              Start
            </button>
            <button disabled={busy || s.state !== "RUNNING"} onClick={() => act("/api/engine/stop")}>
              Stop
            </button>
            <button className="danger" disabled={busy || !r || s.state === "KILLED"} onClick={kill}>
              KILL
            </button>
            {latched && (
              <button disabled={busy} onClick={() => act("/api/engine/reset")}>
                Reset {s.state.replace("_", " ")}
              </button>
            )}
          </div>
          {!r && (
            <p className="hint">
              No run yet. Use <a href="#/start">Start</a> to choose capital, target and pairs.
            </p>
          )}
          <ErrorBox error={error} />
        </section>

        <section className="card">
          <h2>Equity against target {s.mode === "PAPER" && <span className="muted">(simulated)</span>}</h2>
          {r ? (
            <>
              <div style={{ fontSize: 26, fontWeight: 700 }}>
                {eur(r.equity)} <span className="muted" style={{ fontSize: 15 }}>/ {eur(r.target_equity)}</span>
              </div>
              <div className="progress" aria-label="progress to target">
                <div style={{ width: `${Math.max(0, Math.min(100, r.progress_pct))}%` }} />
              </div>
              <dl className="kv">
                <dt>Progress</dt>
                <dd>{r.progress_pct.toFixed(1)}% of the way from {eur(r.starting_capital)}</dd>
                <dt>Cash</dt>
                <dd>{eur(r.cash)}</dd>
                <dt>In positions</dt>
                <dd>{eur(r.positions_value)}</dd>
                <dt>Peak</dt>
                <dd>
                  {eur(r.peak_equity)} · drawdown {r.drawdown_pct.toFixed(2)}%
                </dd>
                <dt>Fees paid</dt>
                <dd>{eur(r.fees_eur, 4)}</dd>
                <dt>Run</dt>
                <dd>
                  #{r.id} since {brussels(r.created_at)} · {r.pairs.join(", ")}
                </dd>
              </dl>
            </>
          ) : (
            <p className="muted">No run.</p>
          )}
        </section>
      </div>

      <section className="card" style={{ marginTop: 16 }}>
        <h2>Open positions</h2>
        {s.positions.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th className="num">Amount</th>
                  <th className="num">Entry</th>
                  <th className="num">Bid</th>
                  <th className="num">Stop</th>
                  <th className="num">Take-profit</th>
                  <th className="num">Unrealised</th>
                  <th className="num">Age (h)</th>
                  <th>Opened</th>
                </tr>
              </thead>
              <tbody>
                {s.positions.map((p) => (
                  <tr key={p.id}>
                    <td>{p.pair}</td>
                    <td className="num">{num(p.amount)}</td>
                    <td className="num">{num(p.entry_price)}</td>
                    <td className="num">{num(p.bid)}</td>
                    <td className="num">{num(p.stop_loss)}</td>
                    <td className="num">{num(p.take_profit)}</td>
                    <td className="num">
                      <Pnl value={p.unrealised_pnl} digits={4} />
                    </td>
                    <td className="num">{p.age_hours.toFixed(1)}</td>
                    <td>{brussels(p.opened_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card" style={{ marginTop: 16 }}>
        <h2>Last decision</h2>
        {s.last_decision ? <DecisionDetail d={s.last_decision} /> : <p className="muted">No decisions yet.</p>}
      </section>
    </>
  );
}
