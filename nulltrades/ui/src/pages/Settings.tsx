import { useEffect, useState } from "react";
import { api, num, Status } from "../api";
import { ErrorBox, PageTitle, useAction } from "../components/common";

type Bound = ["max", number] | ["min", number] | ["range", number, number];

interface SettingsResp {
  settings: {
    pairs: string[];
    timeframe: string;
    model: string;
    prompt_version: string;
    max_model_calls_per_day: number;
    flatten_on_kill: boolean;
    live_capital_cap_eur: number;
    risk: Record<string, number>;
    notifications: { telegram: boolean; daily_summary: boolean };
  };
  bounds: Record<string, Bound>;
  prompt_versions: string[];
  timeframes: string[];
  live_requires_restart: boolean;
}

interface MarketRow {
  pair: string;
  known: boolean;
  active: boolean;
  min_amount: number | null;
  min_cost_eur: number | null;
  price: number | null;
  min_order_eur: number | null;
  size_at_widest_stop_eur: number;
  max_allowed_size_eur: number;
  fits: boolean | null;
}

const RISK_LABELS: Record<string, string> = {
  risk_per_trade_pct: "Risk per trade (%)",
  max_position_pct: "Max position (% of equity)",
  max_open_positions: "Max open positions",
  max_trades_per_day: "Max entries per day",
  min_stop_pct: "Min stop distance (%)",
  max_stop_pct: "Max stop distance (%)",
  max_take_profit_pct: "Max take-profit distance (%)",
  min_reward_risk: "Min reward-to-risk",
  cost_multiple: "Take-profit ≥ N × round-trip fees",
  fee_maker_pct: "Maker fee (%)",
  fee_taker_pct: "Taker fee (%)",
  cooldown_minutes: "Cooldown after stop-out (min)",
  max_daily_loss_pct: "Daily loss budget (% of capital)",
  max_drawdown_pct: "Kill at drawdown (%)",
  max_hold_hours: "Max hold (hours)",
  max_slippage_bps: "LIVE limit slippage (bps)",
  paper_slippage_bps: "PAPER modelled slippage (bps)",
  order_timeout_seconds: "Order timeout (s)",
};

function boundText(b: Bound | undefined): string {
  if (!b) return "";
  if (b[0] === "max") return `ceiling ${b[1]} (tighten only)`;
  if (b[0] === "min") return `floor ${b[1]} (tighten only)`;
  return `${b[1]}–${b[2]}`;
}

export default function Settings({ status, refresh }: { status: Status; refresh: () => void }) {
  const [data, setData] = useState<SettingsResp | null>(null);
  const [draft, setDraft] = useState<SettingsResp["settings"] | null>(null);
  const [newPair, setNewPair] = useState("");
  const [markets, setMarkets] = useState<MarketRow[]>([]);
  const [marketsError, setMarketsError] = useState<string | null>(null);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const [resetText, setResetText] = useState("");
  const [saved, setSaved] = useState(false);
  const save = useAction();
  const reset = useAction();

  const load = () =>
    api.get<SettingsResp>("/api/settings").then((r) => {
      setData(r);
      setDraft(structuredClone(r.settings));
    });

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!draft || draft.pairs.length === 0) {
      setMarkets([]);
      return;
    }
    api
      .get<{ pairs: MarketRow[] }>(`/api/markets?pairs=${encodeURIComponent(draft.pairs.join(","))}`)
      .then((r) => {
        setMarkets(r.pairs);
        setMarketsError(null);
      })
      .catch((e) => setMarketsError(String(e.message ?? e)));
  }, [draft?.pairs.join(",")]);

  if (!data || !draft) return <p className="muted">Loading…</p>;

  const live = status.mode === "LIVE";
  const running = status.state === "RUNNING";
  const set = <K extends keyof SettingsResp["settings"]>(k: K, v: SettingsResp["settings"][K]) =>
    setDraft({ ...draft, [k]: v });

  const addPair = () => {
    const p = newPair.trim().toUpperCase();
    if (p && !draft.pairs.includes(p)) set("pairs", [...draft.pairs, p]);
    setNewPair("");
  };

  const submit = () =>
    save.run(async () => {
      const { live_capital_cap_eur: _cap, ...rest } = draft;
      void _cap;
      await api.put("/api/settings", { settings: rest, confirm_restart: confirmRestart });
      setSaved(true);
      await load();
      refresh();
    });

  return (
    <>
      <PageTitle mode={status.mode}>Settings</PageTitle>
      {running && <div className="error-box">Stop the engine before changing settings.</div>}
      {live && (
        <div className="error-box">LIVE mode: saving settings stops trading until the app is restarted.</div>
      )}

      <div className="grid">
        <section className="card">
          <h2>Pairs (EUR spot whitelist)</h2>
          <div className="btns" style={{ marginBottom: 10 }}>
            <input
              placeholder="e.g. BTC/EUR"
              value={newPair}
              onChange={(e) => setNewPair(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addPair()}
            />
            <button onClick={addPair}>Add</button>
          </div>
          {marketsError && <div className="error-box">{marketsError}</div>}
          {draft.pairs.length === 0 ? (
            <p className="hint">No pairs. The engine will not start without at least one.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Pair</th>
                    <th className="num">Min amount</th>
                    <th className="num">Min order €</th>
                    <th>Fits position size?</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {draft.pairs.map((p) => {
                    const m = markets.find((x) => x.pair === p);
                    return (
                      <tr key={p}>
                        <td>{p}</td>
                        <td className="num">{num(m?.min_amount)}</td>
                        <td className="num">{m?.min_order_eur != null ? `€${m.min_order_eur.toFixed(2)}` : "—"}</td>
                        <td>
                          {!m ? (
                            "…"
                          ) : !m.known ? (
                            <span className="neg">not a Kraken EUR market</span>
                          ) : m.fits === null ? (
                            <span className="muted">no price</span>
                          ) : m.fits ? (
                            <span className="pos">✓ yes (≤ €{m.size_at_widest_stop_eur} at a 5% stop)</span>
                          ) : (
                            <span className="neg">✕ no: minimum above €{m.size_at_widest_stop_eur}</span>
                          )}
                        </td>
                        <td>
                          <button onClick={() => set("pairs", draft.pairs.filter((x) => x !== p))}>Remove</button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="card">
          <h2>Model and loop</h2>
          <form onSubmit={(e) => e.preventDefault()}>
            <div className="row">
              <label htmlFor="tf">Timeframe (one model call per closed candle)</label>
              <select id="tf" value={draft.timeframe} onChange={(e) => set("timeframe", e.target.value)}>
                {data.timeframes.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </div>
            <div className="row">
              <label htmlFor="model">Model</label>
              <input id="model" value={draft.model} onChange={(e) => set("model", e.target.value)} />
            </div>
            <div className="row">
              <label htmlFor="pv">Prompt version</label>
              <select id="pv" value={draft.prompt_version} onChange={(e) => set("prompt_version", e.target.value)}>
                {data.prompt_versions.map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
              <span className="hint">Every decision records the prompt version and model that produced it.</span>
            </div>
            <div className="row">
              <label htmlFor="calls">Max model calls per day (ceiling 48)</label>
              <input
                id="calls"
                type="number"
                min={1}
                max={48}
                value={draft.max_model_calls_per_day}
                onChange={(e) => set("max_model_calls_per_day", Number(e.target.value))}
              />
            </div>
            <label className="check">
              <input type="checkbox" checked={draft.flatten_on_kill} onChange={(e) => set("flatten_on_kill", e.target.checked)} />
              Market-sell all positions on kill
            </label>
            <h3>Notifications</h3>
            <label className="check">
              <input
                type="checkbox"
                checked={draft.notifications.telegram}
                onChange={(e) => set("notifications", { ...draft.notifications, telegram: e.target.checked })}
              />
              Telegram alerts {status.secrets.TELEGRAM_BOT_TOKEN ? "" : "(token not set)"}
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={draft.notifications.daily_summary}
                onChange={(e) => set("notifications", { ...draft.notifications, daily_summary: e.target.checked })}
              />
              Daily summary at 22:00 Brussels
            </label>
            <p className="hint">LIVE capital cap: €{draft.live_capital_cap_eur} (config.yaml only).</p>
          </form>
        </section>
      </div>

      <section className="card" style={{ marginTop: 16 }}>
        <h2>Risk limits</h2>
        <p className="hint">
          Hard bounds live in code. You can tighten a limit, never loosen it past the shipped default. Fee rates
          should match your Kraken fee tier.
        </p>
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12 }}>
          {Object.keys(RISK_LABELS).map((k) => (
            <div className="row" key={k} style={{ display: "grid", gap: 4 }}>
              <label htmlFor={k}>{RISK_LABELS[k]}</label>
              <input
                id={k}
                type="number"
                step="any"
                value={draft.risk[k]}
                onChange={(e) => set("risk", { ...draft.risk, [k]: Number(e.target.value) })}
              />
              <span className="hint">{boundText(data.bounds[k])}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="card" style={{ marginTop: 16 }}>
        {live && (
          <label className="check" style={{ marginBottom: 10 }}>
            <input type="checkbox" checked={confirmRestart} onChange={(e) => setConfirmRestart(e.target.checked)} />
            I understand LIVE trading stays blocked until the app restarts.
          </label>
        )}
        <button className="primary" disabled={save.busy || running || (live && !confirmRestart)} onClick={submit}>
          Save settings
        </button>
        {saved && !save.error && <span className="pos"> Saved to config.yaml.</span>}
        <ErrorBox error={save.error} />
      </section>

      {status.mode === "PAPER" && (
        <section className="card" style={{ marginTop: 16 }}>
          <h2>Reset PAPER state</h2>
          <p className="hint">
            Clears PAPER runs, positions, orders, fills and equity history, and with them the live-gate progress.
            Decisions and events are kept as audit. LIVE state is never touched.
          </p>
          <div className="btns">
            <input placeholder='Type "RESET PAPER"' value={resetText} onChange={(e) => setResetText(e.target.value)} />
            <button
              disabled={reset.busy || resetText !== "RESET PAPER"}
              onClick={() =>
                reset.run(async () => {
                  await api.post("/api/paper/reset", { confirm: resetText });
                  setResetText("");
                  refresh();
                })
              }
            >
              Reset paper
            </button>
          </div>
          <ErrorBox error={reset.error} />
        </section>
      )}
    </>
  );
}
