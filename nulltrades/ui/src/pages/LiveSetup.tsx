import { useEffect, useState } from "react";
import { api, ApiError, Status } from "../api";
import { ErrorBox, PageTitle, useAction } from "../components/common";

interface Condition {
  key: string;
  ok: boolean;
  detail: string;
}

interface Gate {
  ok: boolean;
  conditions: Condition[];
  paper_days: number;
  paper_trades: number;
  paper_net_pnl_eur: number;
  paper_fees_eur: number;
  phrase: string;
  live_capital_cap_eur: number;
  key_permissions: string[];
}

export default function LiveSetup({ status, refresh }: { status: Status; refresh: () => void }) {
  const [gate, setGate] = useState<Gate | null>(null);
  const [phrase, setPhrase] = useState("");
  const [tradeOnly, setTradeOnly] = useState(false);
  const [result, setResult] = useState<Condition[] | null>(null);
  const { busy, error, run, setError } = useAction();

  const load = () => api.get<Gate>("/api/live-gate").then(setGate);
  useEffect(() => {
    load();
  }, [status.mode]);

  const enable = () =>
    run(async () => {
      setResult(null);
      try {
        await api.post("/api/mode", {
          mode: "LIVE",
          phrase,
          trade_only_key_confirmed: tradeOnly,
          acknowledged_paper_pnl_eur: gate?.paper_net_pnl_eur ?? null,
        });
        refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status === 403) {
          const g = (e.body as { gate?: Gate }).gate;
          setResult(g?.conditions ?? null);
          setError("Live gate not passed. Every condition must hold.");
          return;
        }
        throw e;
      }
    });

  const toPaper = () =>
    run(async () => {
      await api.post("/api/mode", { mode: "PAPER" });
      refresh();
    });

  if (status.mode === "LIVE") {
    return (
      <>
        <PageTitle mode="LIVE">Mode</PageTitle>
        <section className="card" style={{ maxWidth: 640 }}>
          <p>
            LIVE is enabled. Orders use real money, capped at €{gate?.live_capital_cap_eur ?? "—"} deployed.
          </p>
          <button disabled={busy || status.state === "RUNNING"} onClick={toPaper}>
            Switch back to PAPER
          </button>
          {status.state === "RUNNING" && <p className="hint">Stop the engine first.</p>}
          <ErrorBox error={error} />
        </section>
      </>
    );
  }

  const conds = result ?? gate?.conditions ?? [];

  return (
    <>
      <PageTitle mode="PAPER">Enable LIVE</PageTitle>
      {!gate ? (
        <p className="muted">Loading…</p>
      ) : (
        <div className="grid">
          <section className="card">
            <h2>Paper record (SIMULATED)</h2>
            <div className="tiles">
              <div className="tile">
                <div className="label">Paper net P&amp;L after fees</div>
                <div className={`value ${gate.paper_net_pnl_eur >= 0 ? "pos" : "neg"}`}>
                  {gate.paper_net_pnl_eur >= 0 ? "+" : "−"}€{Math.abs(gate.paper_net_pnl_eur).toFixed(2)}
                </div>
                <div className="sub">simulated · fees €{gate.paper_fees_eur.toFixed(2)}</div>
              </div>
              <div className="tile">
                <div className="label">Paper run time</div>
                <div className="value">{gate.paper_days.toFixed(1)} d</div>
              </div>
              <div className="tile">
                <div className="label">Closed paper trades</div>
                <div className="value">{gate.paper_trades}</div>
              </div>
            </div>
            <p className="hint">
              Paper fills are immediate at modelled prices, which is optimistic on thin books. Live results will be
              worse.
            </p>
            <h3>Gate conditions</h3>
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {conds.map((c) => (
                <li key={c.key} className={c.ok ? "pos" : "neg"}>
                  {c.ok ? "✓" : "✕"} {c.detail}
                </li>
              ))}
            </ul>
            <p className="hint">The balance query and confirmations are checked when you press Enable.</p>
          </section>

          <section className="card">
            <h2>Kraken API key</h2>
            <p>
              Set <code>KRAKEN_API_KEY</code> and <code>KRAKEN_API_SECRET</code> in the environment. Create the key
              with these permissions only:
            </p>
            <ul>
              {gate.key_permissions.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
            <p className="neg">
              Never enable Withdraw Funds, Deposit or any transfer permission. nulltrades has no code path that uses
              them.
            </p>
            <label className="check" style={{ margin: "10px 0" }}>
              <input type="checkbox" checked={tradeOnly} onChange={(e) => setTradeOnly(e.target.checked)} />I confirm
              this API key has no withdrawal permission.
            </label>
            <div className="row" style={{ display: "grid", gap: 4, marginBottom: 12 }}>
              <label htmlFor="phrase">
                Type <code>{gate.phrase}</code>
              </label>
              <input id="phrase" value={phrase} onChange={(e) => setPhrase(e.target.value)} autoComplete="off" />
            </div>
            <button className="danger" disabled={busy || status.state === "RUNNING"} onClick={enable}>
              Enable LIVE (real money)
            </button>
            {status.state === "RUNNING" && <p className="hint">Stop the engine first.</p>}
            <ErrorBox error={error} />
          </section>
        </div>
      )}
    </>
  );
}
