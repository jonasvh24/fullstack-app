import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, errorMessage } from "../api/client";
import type { ProjectSummary } from "../api/types";
import { ErrorText } from "../components/ErrorText";
import { dateTime, tc } from "../lib/format";
import { href, navigate } from "../lib/router";

function NewProjectForm() {
  const [sourceDir, setSourceDir] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const dir = sourceDir.trim();
    if (!dir) {
      setError("Source folder is required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.createProject({ source_dir: dir, ...(name.trim() ? { name: name.trim() } : {}) });
      navigate(`/p/${encodeURIComponent(res.project.id)}/clips`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <form className="panel new-project" onSubmit={submit} aria-labelledby="np-title">
      <h2 id="np-title" className="section-title">
        New project
      </h2>
      <div className="form-row">
        <label className="field grow">
          <span className="field-label">Source folder (on the server)</span>
          <input
            className="input mono"
            type="text"
            value={sourceDir}
            onChange={(e) => setSourceDir(e.target.value)}
            placeholder="/home/me/footage/lisbon"
            spellCheck={false}
            autoComplete="off"
            required
          />
        </label>
        <label className="field">
          <span className="field-label">Name (optional)</span>
          <input
            className="input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Folder name if blank"
          />
        </label>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Creating" : "Create and ingest"}
        </button>
      </div>
      <ErrorText error={error} />
    </form>
  );
}

export function ProjectsPage() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.listProjects();
      list.sort((a, b) => (a.updated < b.updated ? 1 : -1));
      setProjects(list);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title">Projects</h1>
        <span className="dim mono">{projects ? `${projects.length}` : ""}</span>
      </div>
      <NewProjectForm />
      <section className="panel" aria-label="Project list">
        <ErrorText error={error} />
        {projects && projects.length === 0 && <p className="empty">No projects. Point one at a folder of footage.</p>}
        {projects && projects.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Source folder</th>
                  <th scope="col" className="num">
                    Clips
                  </th>
                  <th scope="col" className="num">
                    Duration
                  </th>
                  <th scope="col" className="num">
                    Updated
                  </th>
                </tr>
              </thead>
              <tbody>
                {projects.map((p) => (
                  <tr key={p.id} className="row-link" onClick={() => navigate(`/p/${encodeURIComponent(p.id)}`)}>
                    <td>
                      <a href={href(`/p/${encodeURIComponent(p.id)}`)} onClick={(e) => e.stopPropagation()}>
                        {p.name}
                      </a>
                    </td>
                    <td className="mono dim truncate" title={p.source_dir}>
                      {p.source_dir}
                    </td>
                    <td className="num mono">{p.clips}</td>
                    <td className="num mono">{tc(p.duration)}</td>
                    <td className="num mono dim">{dateTime(p.updated)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!projects && !error && <p className="dim">Loading</p>}
      </section>
    </div>
  );
}
