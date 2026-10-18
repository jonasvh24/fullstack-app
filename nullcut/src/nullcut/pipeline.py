"""The seven-step flow of plan §2 as callable steps, shared by the CLI and the HTTP server.

Each step reads the previous step's versioned file and writes its own, so any
step can be re-run alone: new music re-runs compile and render, a new brief
re-runs plan onwards, analysis never re-runs for a clip it has seen.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from . import analyse, captions, composer, music, package, plan, qc, render, styles, timeline
from .compiler import pick_fps
from .project import Project, read_json, stable_hash, write_json

Progress = Callable[[float, str], None]
Log = Callable[[str], None]


def _noop(*_a) -> None:
    pass


def ingest(project: Project, folder: Path, progress: Progress = _noop, log: Log = _noop) -> dict:
    return analyse.ingest(project, folder, log=log, progress=progress)


def make_plan(project: Project, answers: dict | None = None, progress: Progress = _noop,
              log: Log = _noop) -> dict:
    progress(0.05, "planning")
    doc = plan.make_plan(project, answers, log=lambda m: (log(m), progress(0.5, m)))
    progress(1.0, "plan ready" if not doc["questions"] else "planner has questions")
    return doc


def compile_timeline(project: Project, log: Log = _noop) -> tuple[int, Path, dict]:
    doc = plan.latest_plan(project, with_beats=True)
    if doc is None or not doc["beats"]:
        raise ValueError("no plan with beats yet: plan the edit first")
    index = project.clip_index()
    brief = plan.load_brief(project)
    fps = pick_fps(index)
    library = music.load_library()
    comp = composer.compose(index, doc, brief, fps, library)
    style = styles.apply_extras(styles.get(doc["style"]), brief["extras"])
    meta = {
        "name": f"{project.manifest()['name']} edit", "stem": "edit", "fps": fps,
        "plan_version": doc["version"], "style": doc["style"], "captions": comp.captions,
        "beats": comp.beats, "zooms": comp.zooms, "broll": comp.broll, "music": comp.music,
        "sfx": comp.sfx, "colour": comp.colour, "report": comp.report,
        "emphasis": {b["id"]: b.get("emphasis", []) for b in doc["beats"]},
        "target_s": brief.get("target_s"), "tolerance": brief["tolerance"],
        "music_wanted": brief["music"]["mode"] != "none" and style["music"]["presence"],
    }
    meta["input_hash"] = stable_hash({"index": read_json(project.clip_index_path),
                                      "plan": doc["beats"], "brief": brief, "meta": meta})
    version, path, created = timeline.save(project, comp.items, index, meta)
    log(f"timeline v{version}{'' if created else ' (unchanged)'}")
    return version, path, meta


def _render_record(project: Project, kind: str, version: int, result: dict, meta: dict,
                   items, fps: float) -> dict:
    style = styles.apply_extras(styles.get(meta["style"]), plan.load_brief(project)["extras"])
    words = captions.output_words(items, fps, meta.get("emphasis"))
    cap = meta["captions"]
    blocks = qc.caption_block_sizes(words, cap["style"], cap["max_words"]) if cap["enabled"] \
        else None
    gate = qc.gate1(result, meta.get("target_s"), meta.get("tolerance", 0.05))
    match = qc.style_match(meta["report"], style, blocks, cap["enabled"],
                           bool(meta.get("music")), meta.get("music_wanted", False))
    rel = Path(result["output"]).relative_to(project.root)
    record = {"kind": kind, "version": version, "path": str(rel), "duration_s": result["video_s"],
              "audio_s": result["audio_s"], "encoded": result["encoded"],
              "cached": result["cached"], "srt": str(Path(result["srt"]).relative_to(project.root)),
              "vtt": str(Path(result["vtt"]).relative_to(project.root)),
              "qc": {**gate, "style_match": match}}
    write_json(project.path("renders", f"edit.v{version}.{kind}.json"), record)
    return record


def render_edit(project: Project, quality: str, progress: Progress = _noop,
                log: Log = _noop) -> dict:
    progress(0.02, "compiling timeline")
    version, path, _ = compile_timeline(project, log)
    meta, items = timeline.load(path)
    result = render.render(project, path, quality, log=log,
                           progress=lambda f, m: progress(0.05 + 0.9 * f, m))
    progress(0.97, "quality checks")
    record = _render_record(project, quality, version, result, meta, items, meta["fps"])
    progress(1.0, f"{quality} ready")
    return record


def master(project: Project, progress: Progress = _noop, log: Log = _noop) -> dict:
    record = render_edit(project, "master", lambda f, m: progress(0.9 * f, m), log)
    progress(0.92, "titles, description and chapters")
    version = record["version"]
    path = project.path("timelines", f"timeline.v{version}.otio")
    meta, items = timeline.load(path)
    index = project.clip_index()
    words = captions.output_words(items, meta["fps"])
    pool = plan.segment_pool(index)
    doc = plan.latest_plan(project, with_beats=True)
    beats = [{**b, **next((x for x in meta["beats"] if x["id"] == b["id"]), {})}
             for b in doc["beats"] if any(x["id"] == b["id"] for x in meta["beats"])]
    language = next((c["language"] for c in index["clips"] if c["words"]), "en")
    pkg = package.build(plan.load_brief(project), beats, words, pool,
                        record["duration_s"], meta.get("music"), language)
    out = project.path("package")
    shutil.copyfile(path, out / f"edit.v{version}.otio")
    pkg["files"] = {"mp4": record["path"], "srt": record["srt"], "vtt": record["vtt"],
                    "otio": f"package/edit.v{version}.otio",
                    "fcpxml": _fcpxml(path, out / f"edit.v{version}.fcpxml", project)}
    pkg["version"] = version
    write_json(out / "package.json", pkg)
    (out / "description.md").write_text(pkg["description"] + "\n")
    write_json(out / "titles.json", pkg["titles"])
    progress(1.0, "master and package ready")
    return {"render": record, "package": pkg}


def _fcpxml(otio_path: Path, out: Path, project: Project) -> str | None:
    """FCPXML for Final Cut and Resolve, when the OTIO adapter plugin is installed."""
    try:
        import opentimelineio as otio

        tl = otio.adapters.read_from_file(str(otio_path))
        otio.adapters.write_to_file(tl, str(out), adapter_name="fcpx_xml")
        return str(out.relative_to(project.root))
    except Exception:  # adapter missing or the timeline holds something it cannot express
        return None
