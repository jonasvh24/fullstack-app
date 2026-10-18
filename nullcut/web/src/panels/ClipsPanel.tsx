import { JobProgress } from "../components/JobProgress";
import { Thumb } from "../components/Thumb";
import { isActive, latestOfKind } from "../lib/jobs";
import { resolution, tc } from "../lib/format";
import { href } from "../lib/router";
import type { ProjectContext } from "../pages/ProjectPage";

export function ClipsPanel({ ctx }: { ctx: ProjectContext }) {
  const { project, jobs } = ctx;
  const ingest = latestOfKind(jobs, "ingest");
  const clips = project.clips;
  const total = clips.reduce((a, c) => a + c.duration, 0);
  const speech = clips.filter((c) => c.speech).length;

  return (
    <section className="panel" aria-labelledby="clips-title">
      <div className="panel-head">
        <h2 id="clips-title" className="section-title">
          Clips
        </h2>
        <span className="stats mono">
          <span>{clips.length} clips</span>
          <span>{tc(total)} total</span>
          <span>{speech} speech</span>
          <span>{clips.length - speech} B-roll</span>
        </span>
        <span className="spacer" />
        {clips.length > 0 && (
          <a className="btn" href={href(`/p/${encodeURIComponent(project.id)}/brief`)}>
            Write the brief
          </a>
        )}
      </div>

      {ingest && (isActive(ingest) || ingest.status === "failed") && <JobProgress job={ingest} />}

      {clips.length === 0 ? (
        <p className="empty">{isActive(ingest) ? "Ingest running. Clips appear as they are analysed." : "No clips in this project."}</p>
      ) : (
        <div className="table-wrap">
          <table className="table clips-table">
            <thead>
              <tr>
                <th scope="col">
                  <span className="sr-only">Thumbnail</span>
                </th>
                <th scope="col">Name</th>
                <th scope="col" className="num">
                  Duration
                </th>
                <th scope="col">Format</th>
                <th scope="col">Lang</th>
                <th scope="col">ASR</th>
                <th scope="col" className="num">
                  Words
                </th>
                <th scope="col" className="num">
                  Shots
                </th>
                <th scope="col">Type</th>
              </tr>
            </thead>
            <tbody>
              {clips.map((c) => (
                <tr key={c.id}>
                  <td className="cell-thumb">
                    <Thumb src={c.thumb_url} width={80} />
                  </td>
                  <td className="truncate" title={c.name}>
                    {c.name}
                  </td>
                  <td className="num mono">{tc(c.duration)}</td>
                  <td className="mono dim">{resolution(c.width, c.height, c.fps)}</td>
                  <td className="mono">{c.language || "–"}</td>
                  <td className="mono dim">{c.asr || "–"}</td>
                  <td className="num mono">{c.words}</td>
                  <td className="num mono">{c.shots}</td>
                  <td>
                    <span className={`tag ${c.speech ? "tag-speech" : "tag-broll"}`}>{c.speech ? "speech" : "B-roll"}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
