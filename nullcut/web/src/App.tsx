import { HealthStrip } from "./components/HealthStrip";
import { STEPS, type Step } from "./components/StepBar";
import { ProjectPage } from "./pages/ProjectPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { StylesPage } from "./pages/StylesPage";
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
        <nav className="topnav" aria-label="Main">
          <a href={href("/")} aria-current={route.name === "projects" || route.name === "project" ? "page" : undefined}>
            Projects
          </a>
          <a href={href("/styles")} aria-current={route.name === "styles" ? "page" : undefined}>
            Styles
          </a>
        </nav>
      </header>
      <main className="main">
        {route.name === "projects" && <ProjectsPage />}
        {route.name === "styles" && <StylesPage />}
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
