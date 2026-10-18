import { HealthStrip } from "./components/HealthStrip";
import { STEPS, type Step } from "./components/StepBar";
import { ProjectPage } from "./pages/ProjectPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { href, matchRoute, useHashPath } from "./lib/router";

function isStep(s: string | null): s is Step {
  return !!s && (STEPS as readonly string[]).includes(s);
}

export function App() {
  const route = matchRoute(useHashPath());
  return (
    <div className="shell">
      <header className="topbar">
        <a className="wordmark" href={href("/")}>
          nullcut
        </a>
        <span className="topbar-sub">local editor</span>
      </header>
      <main className="main">
        {route.name === "projects" && <ProjectsPage />}
        {route.name === "project" && (
          <ProjectPage key={route.id} id={route.id} step={isStep(route.step) ? route.step : "clips"} />
        )}
        {route.name === "notFound" && (
          <div className="page">
            <p>
              No page at <span className="mono">{route.path}</span>. <a href={href("/")}>Projects</a>
            </p>
          </div>
        )}
      </main>
      <HealthStrip />
    </div>
  );
}
