"""HTTP API for the local UI (docs/api.md). One user, localhost, no auth (plan §1).

Every long operation is a job; every response is a view over the versioned
files the pipeline writes, so the server holds no state a restart could lose.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, llm, media, models, music, pipeline, plan, styles, timeline
from .jobs import JobStore
from .project import Project, natural_key, open_or_create, read_json

WEB_DIST = Path(__file__).parent / "web_dist"


def create_app(home: Path | None = None) -> FastAPI:
    home = home or config.nullcut_home()
    jobs = JobStore(home / "jobs.sqlite")
    app = FastAPI(title="nullcut", version=__version__)
    projects_dir = home / "projects"

    # --- helpers -----------------------------------------------------------------

    def project(pid: str) -> Project:
        if "/" in pid or pid in (".", "..") or not (projects_dir / pid / "project.json").exists():
            raise HTTPException(404, f"no project {pid}")
        return Project(projects_dir / pid)

    def media_url(p: Project, rel: str | None) -> str | None:
        return f"/media/{p.id}/{rel}" if rel else None

    def segment_view(p: Project, seg: dict) -> dict:
        return {"id": seg["id"], "clip_id": seg["clip_id"], "start": seg["start"],
                "end": seg["end"], "text": seg["text"], "score": seg["score"],
                "thumb_url": media_url(p, seg.get("keyframe"))}

    def clip_view(p: Project, c: dict) -> dict:
        kf = c.get("keyframes") or {}
        mid = sorted(kf.items(), key=lambda kv: int(kv[0]))
        thumb = mid[len(mid) // 2][1] if mid else None
        return {"id": c["id"], "name": c["name"], "duration": c["duration"],
                "width": c["width"], "height": c["height"], "fps": c["fps"],
                "language": c["language"], "asr": c["asr"], "words": len(c["words"]),
                "speech": bool(c["words"]), "thumb_url": media_url(p, thumb),
                "shots": len(c.get("shots", []))}

    def plan_view(p: Project, doc: dict) -> dict:
        pool = plan.segment_pool(p.clip_index())
        beats = []
        for b in doc["beats"]:
            beats.append({
                "id": b["id"], "role": b["role"], "title": b["title"],
                "intent": b.get("intent", ""),
                "segments": [segment_view(p, pool[s]) for s in b["segment_ids"] if s in pool],
                "dropped": [segment_view(p, pool[s]) for s in b.get("dropped_ids", [])
                            if s in pool],
                "duration_s": b.get("duration_s", 0.0), "target_s": b.get("target_s"),
                "emphasis": b.get("emphasis", []), "music_energy": b.get("music_energy", "mid"),
                "locked": b.get("locked", False)})
        sol = doc.get("solver", {})
        return {"version": doc["version"], "planner": doc["planner"], "style": doc["style"],
                "summary": doc.get("summary", ""), "questions": doc.get("questions", []),
                "beats": beats,
                "solver": {"target_s": sol.get("target_s"), "kept_s": sol.get("kept_s", 0.0),
                           "status": sol.get("status", "no_target"),
                           "shortfall_s": sol.get("shortfall_s", 0.0)}}

    def render_view(p: Project, rec: dict) -> dict:
        return {"kind": rec["kind"], "version": rec["version"], "url": media_url(p, rec["path"]),
                "duration_s": rec["duration_s"], "qc": rec["qc"]}

    def renders(p: Project) -> list[dict]:
        recs = [read_json(f) for f in p.path("renders").glob("edit.v*.json")]
        recs.sort(key=lambda r: (r["version"], r["kind"] == "master"), reverse=True)
        return recs

    # --- endpoints -----------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        ep, vlm = llm.planner_endpoint(), llm.vlm_endpoint()
        asr = models.DEFAULT_PROFILE["asr"]
        return {"version": __version__,
                "llm": {"url": ep.url if ep else None, "model": ep.model if ep else None,
                        "ok": llm.ping(ep)},
                "vlm": {"url": vlm.url if vlm else None, "ok": llm.ping(vlm)},
                "asr": asr if models.installed(asr) else f"{asr} (not pulled)",
                "encoder": media.video_encoder()}

    @app.get("/api/styles")
    def list_styles() -> list[dict]:
        return [{"id": s["id"], "name": s["name"], "description": s["description"],
                 "params": {k: v for k, v in s.items() if k not in ("id", "name", "description")}}
                for s in styles.all_styles().values()]

    @app.get("/api/music")
    def list_music() -> list[dict]:
        return [{"id": t["id"], "name": t["name"], "duration": t["duration"], "bpm": t["bpm"],
                 "energy": t["energy"], "tags": t["tags"], "licence": t["licence"]}
                for t in music.load_library()]

    @app.post("/api/music/import")
    def import_music(body: dict = Body(...)) -> dict:
        folder = Path(str(body.get("folder", ""))).expanduser()
        if not folder.is_dir():
            raise HTTPException(400, f"{folder} is not a folder on this machine")
        return jobs.submit("_music", "music_import",
                           lambda progress: {"tracks": len(music.import_folder(
                               folder, log=lambda m: None, progress=progress))})

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        out = []
        if projects_dir.is_dir():
            for d in sorted(projects_dir.iterdir(), key=lambda d: natural_key(d.name)):
                mf = d / "project.json"
                if not mf.exists():
                    continue
                m = read_json(mf)
                idx_path = d / "index" / "clip_index.json"
                clips = read_json(idx_path)["clips"] if idx_path.exists() else []
                newest = max((f.stat().st_mtime for f in d.rglob("*.json")), default=0)
                out.append({"id": d.name, "name": m["name"], "source_dir": m["source_dir"],
                            "clips": len(clips), "duration": sum(c["duration"] for c in clips),
                            "updated": datetime.fromtimestamp(newest).isoformat(
                                timespec="seconds")})
        return out

    @app.post("/api/projects")
    def create_project(body: dict = Body(...)) -> dict:
        folder = Path(str(body.get("source_dir", ""))).expanduser()
        if not folder.is_dir():
            raise HTTPException(400, f"{folder} is not a folder on this machine")
        p = open_or_create(home, folder, body.get("name") or None)
        job = jobs.busy(p.id, "ingest") or jobs.submit(
            p.id, "ingest", lambda progress: _ingest(p, folder, progress))
        return {"project": get_project(p.id), "job": job}

    def _ingest(p: Project, folder: Path, progress) -> dict:
        index = pipeline.ingest(p, folder, progress)
        return {"clips": len(index["clips"])}

    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        p = project(pid)
        m = p.manifest()
        clips = [clip_view(p, c) for c in p.clip_index()["clips"]] \
            if p.clip_index_path.exists() else []
        pl = plan.latest_plan(p)
        tl = p.latest("timelines", "timeline", ".otio")
        return {"id": p.id, "name": m["name"], "source_dir": m["source_dir"], "clips": clips,
                "brief": plan.load_brief(p), "jobs": jobs.for_project(p.id),
                "plan_version": pl["version"] if pl else None,
                "timeline_version": tl[0] if tl else None}

    @app.get("/api/projects/{pid}/segments")
    def get_segments(pid: str) -> list[dict]:
        p = project(pid)
        if not p.clip_index_path.exists():
            return []
        return [segment_view(p, s) for s in plan.segment_pool(p.clip_index()).values()]

    @app.put("/api/projects/{pid}/brief")
    def put_brief(pid: str, body: dict = Body(...)) -> dict:
        try:
            return plan.save_brief(project(pid), body)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc).strip("'\"")) from exc

    @app.post("/api/projects/{pid}/plan")
    def post_plan(pid: str, body: dict | None = Body(None)) -> dict:
        p = project(pid)
        if not p.clip_index_path.exists():
            raise HTTPException(409, "ingest has not finished")
        answers = {}
        if body and body.get("answers"):
            # The UI answers by question id; the planner keys answers by question text.
            current = plan.latest_plan(p) or {}
            texts = {q["id"]: q["text"] for q in current.get("questions", [])}
            answers = {texts.get(k, k): v for k, v in body["answers"].items()}
        busy = jobs.busy(p.id, "plan")
        if busy:
            return busy
        return jobs.submit(p.id, "plan", lambda progress: plan_view(
            p, pipeline.make_plan(p, answers, progress)))

    @app.get("/api/projects/{pid}/plan")
    def get_plan(pid: str) -> dict:
        p = project(pid)
        doc = plan.latest_plan(p)
        if doc is None:
            raise HTTPException(404, "no plan yet")
        return plan_view(p, doc)

    @app.put("/api/projects/{pid}/plan")
    def put_plan(pid: str, body: dict = Body(...)) -> dict:
        p = project(pid)
        try:
            return plan_view(p, plan.edit_plan(p, body.get("beats", []), log=lambda m: None))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/projects/{pid}/preview")
    def post_preview(pid: str) -> dict:
        p = project(pid)
        _require_plan(p)
        return jobs.busy(p.id, "preview") or jobs.submit(
            p.id, "preview",
            lambda progress: render_view(p, pipeline.render_edit(p, "preview", progress)))

    @app.post("/api/projects/{pid}/master")
    def post_master(pid: str) -> dict:
        p = project(pid)
        _require_plan(p)

        def run(progress) -> dict:
            out = pipeline.master(p, progress)
            return {"render": render_view(p, out["render"]), "package": package_view(p)}

        return jobs.busy(p.id, "master") or jobs.submit(p.id, "master", run)

    def _require_plan(p: Project) -> None:
        doc = plan.latest_plan(p, with_beats=True)
        if doc is None:
            raise HTTPException(409, "plan the edit first")

    @app.get("/api/projects/{pid}/timeline")
    def get_timeline(pid: str) -> dict:
        p = project(pid)
        latest = p.latest("timelines", "timeline", ".otio")
        if not latest:
            raise HTTPException(404, "no timeline yet")
        meta, items = timeline.load(latest[1])
        fps = meta["fps"]
        cur, rows = 0, []
        for it in items:
            rows.append({"start_s": round(cur / fps, 3), "end_s": round((cur + it.frames) / fps, 3),
                         "clip_id": it.clip_id, "track": "V1", "rules": it.rules})
            cur += it.frames
        for b in meta.get("broll", []):
            rows.append({"start_s": b["start_s"], "end_s": b["end_s"], "clip_id": b["clip_id"],
                         "track": "V2", "rules": [b["rule"]]})
        m = meta.get("music")
        size = config.MASTER_SIZE
        return {"version": latest[0], "fps": fps, "duration_s": round(cur / fps, 3),
                "width": size[0], "height": size[1],
                "beats": [{k: b[k] for k in ("id", "role", "title", "start_s", "end_s")}
                          for b in meta.get("beats", [])],
                "items": rows,
                "zooms": [{"start_s": z["start_s"], "end_s": z["end_s"], "scale": z["scale"],
                           "rule": z["rule"]} for z in meta.get("zooms", [])],
                "music": {"track": m["name"], "start_s": 0.0, "end_s": round(cur / fps, 3)}
                if m else None}

    @app.get("/api/projects/{pid}/renders")
    def get_renders(pid: str) -> list[dict]:
        p = project(pid)
        return [render_view(p, r) for r in renders(p)]

    def package_view(p: Project) -> dict:
        path = p.path("package", "package.json")
        if not path.exists():
            raise HTTPException(404, "no package yet: render the master")
        pkg = read_json(path)
        files = pkg["files"]
        return {"titles": [{k: t[k] for k in ("text", "rationale", "verified")}
                           for t in pkg["titles"]],
                "description": pkg["description"], "chapters": pkg["chapters"],
                "files": {"srt": media_url(p, files["srt"]), "vtt": media_url(p, files["vtt"]),
                          "otio": media_url(p, files["otio"]),
                          "fcpxml": media_url(p, files.get("fcpxml")),
                          "mp4": media_url(p, files["mp4"])}}

    @app.get("/api/projects/{pid}/package")
    def get_package(pid: str) -> dict:
        return package_view(project(pid))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id}")
        return job

    @app.get("/media/{pid}/{rel:path}")
    def get_media(pid: str, rel: str) -> FileResponse:
        p = project(pid)
        target = (p.root / rel).resolve()
        # Only files inside the project, and never the originals' own folders.
        if p.root.resolve() not in target.parents or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="ui")
    return app


def serve(host: str = "127.0.0.1", port: int = 8765, home: str | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(config.nullcut_home(home)), host=host, port=port,
                log_level=os.environ.get("NULLCUT_LOG", "warning"))
