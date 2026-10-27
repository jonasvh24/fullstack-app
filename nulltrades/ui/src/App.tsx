import { useCallback, useEffect, useState } from "react";
import { api, Status } from "./api";
import Dashboard from "./pages/Dashboard";
import StartRun from "./pages/StartRun";
import Settings from "./pages/Settings";
import Decisions from "./pages/Decisions";
import Trades from "./pages/Trades";
import Performance from "./pages/Performance";
import LiveSetup from "./pages/LiveSetup";

const ROUTES = [
  ["dashboard", "Dashboard"],
  ["start", "Start"],
  ["decisions", "Decisions"],
  ["trades", "Trades"],
  ["performance", "Performance"],
  ["settings", "Settings"],
  ["live", "Mode"],
] as const;

type Route = (typeof ROUTES)[number][0];

function currentRoute(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  return (ROUTES.find(([r]) => r === h)?.[0] ?? "dashboard") as Route;
}

function initialTheme(): string {
  try {
    return localStorage.getItem("nulltrades.theme") ?? "dark";
  } catch {
    return "dark";
  }
}

export default function App() {
  const [route, setRoute] = useState<Route>(currentRoute());
  const [status, setStatus] = useState<Status | null>(null);
  const [offline, setOffline] = useState<string | null>(null);
  const [theme, setTheme] = useState(initialTheme());

  useEffect(() => {
    const onHash = () => setRoute(currentRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("nulltrades.theme", theme);
    } catch {
      /* private window: theme just won't persist */
    }
  }, [theme]);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.get<Status>("/api/status"));
      setOffline(null);
    } catch (e) {
      setOffline(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 3000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const mode = status?.mode ?? "PAPER";

  return (
    <>
      <div className={`mode-strip ${mode === "LIVE" ? "live" : "paper"}`} role="status">
        {mode === "LIVE" ? "LIVE · REAL MONEY" : "PAPER · SIMULATED · NO REAL MONEY"}
      </div>
      <header className="top">
        <span className="brand">nulltrades</span>
        <span className={`badge ${mode === "LIVE" ? "live" : "paper"}`}>{mode}</span>
        {status && <span className={`badge state ${status.state}`}>{status.state.replace("_", " ")}</span>}
        <nav>
          {ROUTES.map(([r, label]) => (
            <a key={r} href={`#/${r}`} className={route === r ? "active" : ""}>
              {label}
            </a>
          ))}
        </nav>
        <span className="spacer" />
        <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle theme">
          {theme === "dark" ? "Light" : "Dark"}
        </button>
      </header>
      <main>
        {offline && <div className="error-box">Backend not reachable: {offline}</div>}
        {!status ? (
          <p className="muted">Loading…</p>
        ) : route === "dashboard" ? (
          <Dashboard status={status} refresh={refresh} />
        ) : route === "start" ? (
          <StartRun status={status} refresh={refresh} />
        ) : route === "decisions" ? (
          <Decisions mode={status.mode} />
        ) : route === "trades" ? (
          <Trades mode={status.mode} />
        ) : route === "performance" ? (
          <Performance status={status} />
        ) : route === "settings" ? (
          <Settings status={status} refresh={refresh} />
        ) : (
          <LiveSetup status={status} refresh={refresh} />
        )}
      </main>
    </>
  );
}
