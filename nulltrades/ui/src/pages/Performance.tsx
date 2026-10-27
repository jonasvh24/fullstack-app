import { useEffect, useState } from "react";
import { api, eur, signedEur, Status } from "../api";
import { ErrorBox, PageTitle } from "../components/common";
import LineChart from "../components/LineChart";

interface Perf {
  label: string;
  run: number | null;
  equity_curve?: Array<{ ts: string; equity: number; peak: number; drawdown_pct: number }>;
  max_drawdown_pct?: number;
  trades?: number;
  win_rate_pct?: number | null;
  avg_win_eur?: number | null;
  avg_loss_eur?: number | null;
  expectancy_eur?: number | null;
  net_pnl_eur?: number;
  total_fees_eur?: number;
  buy_and_hold_equity?: number | null;
  buy_and_hold_note?: string;
  starting_capital?: number;
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

export default function Performance({ status }: { status: Status }) {
  const [p, setP] = useState<Perf | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .get<Perf>("/api/performance")
        .then(setP)
        .catch((e) => setError(String(e.message ?? e)));
    load();
    const t = window.setInterval(load, 30000);
    return () => window.clearInterval(t);
  }, [status.mode, status.run?.id]);

  const sim = status.mode === "PAPER" ? " (simulated)" : "";

  return (
    <>
      <PageTitle mode={status.mode}>Performance</PageTitle>
      <ErrorBox error={error} />
      {!p || !p.run ? (
        <p className="muted">No run yet.</p>
      ) : (
        <>
          <section className="card">
            <div className="tiles">
              <Tile label={`Net P&L after fees${sim}`} value={signedEur(p.net_pnl_eur, 4)} sub={`${p.trades} closed trades`} />
              <Tile label="Win rate" value={p.win_rate_pct == null ? "—" : `${p.win_rate_pct}%`} />
              <Tile label="Average win" value={signedEur(p.avg_win_eur, 4)} />
              <Tile label="Average loss" value={signedEur(p.avg_loss_eur, 4)} />
              <Tile label="Expectancy per trade" value={signedEur(p.expectancy_eur, 4)} sub="after fees" />
              <Tile label="Total fees" value={eur(p.total_fees_eur, 4)} />
              <Tile label="Max drawdown" value={`${(p.max_drawdown_pct ?? 0).toFixed(2)}%`} />
              <Tile
                label="Buy-and-hold, same period"
                value={eur(p.buy_and_hold_equity, 2)}
                sub={`from ${eur(p.starting_capital)} · ${p.buy_and_hold_note ?? ""}`}
              />
            </div>
          </section>
          <section className="card" style={{ marginTop: 16 }}>
            <LineChart
              title={`Equity${sim}`}
              points={(p.equity_curve ?? []).map((r) => ({ t: r.ts, v: r.equity }))}
              format={(v) => `€${v.toFixed(2)}`}
              reference={status.run ? { v: status.run.target_equity, label: `target €${status.run.target_equity.toFixed(2)}` } : undefined}
            />
          </section>
          <section className="card" style={{ marginTop: 16 }}>
            <LineChart
              title="Drawdown from peak"
              points={(p.equity_curve ?? []).map((r) => ({ t: r.ts, v: r.drawdown_pct }))}
              format={(v) => `${v.toFixed(1)}%`}
              area
              invert
              height={160}
            />
          </section>
          {(p.equity_curve?.length ?? 0) > 0 && (
            <details className="card" style={{ marginTop: 16 }}>
              <summary>Equity table</summary>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Time (UTC)</th>
                      <th className="num">Equity</th>
                      <th className="num">Peak</th>
                      <th className="num">Drawdown</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.equity_curve!.slice(-200).map((r) => (
                      <tr key={r.ts}>
                        <td>{r.ts}</td>
                        <td className="num">{r.equity.toFixed(4)}</td>
                        <td className="num">{r.peak.toFixed(4)}</td>
                        <td className="num">{r.drawdown_pct.toFixed(2)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}
    </>
  );
}
