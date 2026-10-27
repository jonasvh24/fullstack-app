import { FormEvent, useEffect, useState } from "react";
import { api, Status } from "../api";
import { ErrorBox, PageTitle, useAction } from "../components/common";

interface SettingsResp {
  settings: { pairs: string[]; starting_capital_eur: number; target_roi: number; live_capital_cap_eur: number };
}

export default function StartRun({ status, refresh }: { status: Status; refresh: () => void }) {
  const [capital, setCapital] = useState("20");
  const [roi, setRoi] = useState("1.0");
  const [whitelist, setWhitelist] = useState<string[]>([]);
  const [pairs, setPairs] = useState<string[]>([]);
  const [cap, setCap] = useState<number | null>(null);
  const { busy, error, run } = useAction();
  const [done, setDone] = useState(false);

  useEffect(() => {
    api.get<SettingsResp>("/api/settings").then((r) => {
      setCapital(String(r.settings.starting_capital_eur));
      setRoi(String(r.settings.target_roi));
      setWhitelist(r.settings.pairs);
      setPairs(r.settings.pairs);
      setCap(r.settings.live_capital_cap_eur);
    });
  }, []);

  const toggle = (p: string) => setPairs(pairs.includes(p) ? pairs.filter((x) => x !== p) : [...pairs, p]);
  const running = status.state === "RUNNING";
  const latched = ["KILLED", "TARGET_REACHED", "HALTED"].includes(status.state);
  const target = Number(capital) * (1 + Number(roi));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    run(async () => {
      await api.post("/api/runs", { capital: Number(capital), target_roi: Number(roi), pairs });
      setDone(true);
      refresh();
    });
  };

  return (
    <>
      <PageTitle mode={status.mode}>Start a run</PageTitle>
      <section className="card" style={{ maxWidth: 560 }}>
        <form onSubmit={submit}>
          <div className="row">
            <label>Mode</label>
            <div>
              <span className={`badge ${status.mode === "LIVE" ? "live" : "paper"}`}>{status.mode}</span>{" "}
              <span className="hint">
                The app always starts in PAPER. Switching to LIVE goes through the <a href="#/live">live gate</a>.
              </span>
            </div>
          </div>
          <div className="row">
            <label htmlFor="cap">Starting capital (EUR)</label>
            <input id="cap" type="number" min="1" step="0.01" value={capital} onChange={(e) => setCapital(e.target.value)} />
            {status.mode === "LIVE" && cap !== null && (
              <span className="hint">LIVE never deploys more than €{cap} (live_capital_cap_eur).</span>
            )}
            {status.mode === "PAPER" && <span className="hint">Virtual cash. Nothing real is spent.</span>}
          </div>
          <div className="row">
            <label htmlFor="roi">Target ROI (1.0 = +100%)</label>
            <input id="roi" type="number" min="0.01" step="0.01" value={roi} onChange={(e) => setRoi(e.target.value)} />
            <span className="hint">
              Target equity €{Number.isFinite(target) ? target.toFixed(2) : "—"}. At target the engine closes everything,
              converts to EUR, notifies and stops.
            </span>
          </div>
          <div className="row">
            <label>Pairs</label>
            {whitelist.length === 0 ? (
              <span className="hint">
                No pairs configured. Add EUR pairs in <a href="#/settings">Settings</a> first.
              </span>
            ) : (
              <div className="btns">
                {whitelist.map((p) => (
                  <label key={p} className="check">
                    <input type="checkbox" checked={pairs.includes(p)} onChange={() => toggle(p)} /> {p}
                  </label>
                ))}
              </div>
            )}
          </div>
          {status.run && (
            <p className="hint">
              Starting a new run ends run #{status.run.id}. It needs the engine stopped and no open positions.
            </p>
          )}
          <button className="primary" disabled={busy || running || latched || pairs.length === 0}>
            Create run
          </button>
          {running && <p className="hint">Stop the engine first.</p>}
          {latched && <p className="hint">Engine is {status.state}; reset it on the dashboard first.</p>}
          {done && !error && (
            <p className="pos">
              Run created. Press Start on the <a href="#/dashboard">dashboard</a>.
            </p>
          )}
          <ErrorBox error={error} />
        </form>
      </section>
    </>
  );
}
