import { useEffect, useState } from "react";
import { api, brussels, Mode, num } from "../api";
import { ErrorBox, PageTitle, Pnl } from "../components/common";

interface Fill {
  id: number;
  ts: string;
  pair: string;
  side: string;
  amount: number;
  price: number;
  cost: number;
  fee: number;
  purpose: string;
}

interface Closed {
  id: number;
  pair: string;
  opened_at: string;
  closed_at: string;
  amount: number;
  entry_price: number;
  exit_price: number | null;
  entry_fee: number;
  exit_fee: number | null;
  realised_pnl: number | null;
  exit_reason: string;
}

export default function Trades({ mode }: { mode: Mode }) {
  const [fills, setFills] = useState<Fill[]>([]);
  const [closed, setClosed] = useState<Closed[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<{ fills: Fill[]; closed: Closed[] }>("/api/trades")
      .then((r) => {
        setFills(r.fills);
        setClosed(r.closed);
      })
      .catch((e) => setError(String(e.message ?? e)));
  }, [mode]);

  return (
    <>
      <PageTitle mode={mode}>Trades</PageTitle>
      <div className="btns" style={{ marginBottom: 12 }}>
        <a className="button" href="/api/export/trades.csv">
          Export CSV ({mode === "PAPER" ? "PAPER · SIMULATED" : "LIVE"})
        </a>
      </div>
      <ErrorBox error={error} />
      <section className="card">
        <h2>Closed positions · realised P&amp;L after fees</h2>
        {closed.length === 0 ? (
          <p className="muted">None yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Closed (Brussels)</th>
                  <th>Pair</th>
                  <th>Exit reason</th>
                  <th className="num">Amount</th>
                  <th className="num">Entry</th>
                  <th className="num">Exit</th>
                  <th className="num">Fees €</th>
                  <th className="num">Realised</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((t) => (
                  <tr key={t.id}>
                    <td>{brussels(t.closed_at)}</td>
                    <td>{t.pair}</td>
                    <td>{t.exit_reason}</td>
                    <td className="num">{num(t.amount)}</td>
                    <td className="num">{num(t.entry_price)}</td>
                    <td className="num">{num(t.exit_price)}</td>
                    <td className="num">{(t.entry_fee + (t.exit_fee ?? 0)).toFixed(4)}</td>
                    <td className="num">
                      <Pnl value={t.realised_pnl} digits={4} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card" style={{ marginTop: 16 }}>
        <h2>Fills</h2>
        {fills.length === 0 ? (
          <p className="muted">None yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time (Brussels)</th>
                  <th>Pair</th>
                  <th>Side</th>
                  <th>Purpose</th>
                  <th className="num">Amount</th>
                  <th className="num">Price</th>
                  <th className="num">Cost €</th>
                  <th className="num">Fee €</th>
                </tr>
              </thead>
              <tbody>
                {fills.map((f) => (
                  <tr key={f.id}>
                    <td>{brussels(f.ts)}</td>
                    <td>{f.pair}</td>
                    <td>{f.side}</td>
                    <td>{f.purpose}</td>
                    <td className="num">{num(f.amount)}</td>
                    <td className="num">{num(f.price)}</td>
                    <td className="num">{f.cost.toFixed(4)}</td>
                    <td className="num">{f.fee.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
