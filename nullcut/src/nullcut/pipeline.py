"""The seven-step flow of plan §2 as callable steps, shared by the CLI and the HTTP server.

Each step reads the previous step's versioned file and writes its own, so any
step can be re-run alone: new music re-runs compile and render, a new brief
re-runs plan onwards, analysis never re-runs for a clip it has seen.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from . import (analyse, captions, composer, fx, measure, music, package, plan, qc, render,
               style_engine, styles, thumbnails, timeline)
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
    source_colour = fx.footage_colour(project, index)
    colour_capped = False
    if "target" in comp.colour:
        # An extracted profile stores a look, not a tweak: fit the LUT to this footage.
        target = comp.colour["target"]
        comp.colour = fx.colour_transfer(target, source_colour) if source_colour else {}
        reach = style_engine.expected_colour(comp.colour, source_colour) if comp.colour else None
        colour_capped = bool(reach) and abs(reach["warmth"] - target["warmth"]) > 0.05
    meta = {
        "name": f"{project.manifest()['name']} edit", "stem": "edit", "fps": fps,
        "plan_version": doc["version"], "style": doc["style"], "captions": comp.captions,
        "beats": comp.beats, "zooms": comp.zooms, "broll": comp.broll, "music": comp.music,
        "sfx": comp.sfx, "colour": comp.colour, "report": comp.report,
        "emphasis": {b["id"]: b.get("emphasis", []) for b in doc["beats"]},
        "target_s": brief.get("target_s"), "tolerance": brief["tolerance"],
        "music_wanted": brief["music"]["mode"] != "none" and style["music"]["presence"],
        "source_colour": source_colour,
        "reframe": comp.reframe,
        "colour_capped": colour_capped,
        "expected_colour": fx.graded_colour(project, index, comp.colour or {}),
    }
    meta["input_hash"] = stable_hash({"index": read_json(project.clip_index_path),
                                      "plan": doc["beats"], "brief": brief, "meta": meta})
    version, path, created = timeline.save(project, comp.items, index, meta)
    log(f"timeline v{version}{'' if created else ' (unchanged)'}")
    return version, path, meta


def _render_record(project: Project, kind: str, version: int, result: dict, meta: dict,
                   items, fps: float, fmt: str = "16:9") -> dict:
    style = styles.apply_extras(styles.get(meta["style"]), plan.load_brief(project)["extras"])
    words = captions.output_words(items, fps, meta.get("emphasis"))
    cap = meta["captions"]
    blocks = qc.caption_block_sizes(words, cap["style"], cap["max_words"]) if cap["enabled"] \
        else None
    gate = qc.gate1(result, meta.get("target_s"), meta.get("tolerance", 0.05))
    match = qc.style_match(meta["report"], style, blocks, cap["enabled"],
                           bool(meta.get("music")), meta.get("music_wanted", False))
    # Plan §5: the same measurements that build a profile run on the output.
    measured = measure.measure(Path(result["output"]), words=words)
    # Colour is judged against the grade this footage received: an extracted look is
    # an absolute target that footage of another colour can only approach.
    applied = {**style, "colour": meta.get("colour") or {"saturation": 1.0}}
    detail = style_engine.match(measured, applied, meta.get("music_wanted", False),
                                cap["style"] if cap["enabled"] else "off",
                                meta.get("source_colour"), meta.get("expected_colour"))
    if meta.get("colour_capped") and "colour" in detail:
        detail["colour"]["note"] = (detail["colour"]["note"] or "") + \
            " (the reference look is out of reach for this footage; the grade was capped)"
    match = {k: d["score"] for k, d in detail.items()}
    rel = Path(result["output"]).relative_to(project.root)
    record = {"kind": kind, "format": fmt, "version": version, "path": str(rel),
              "duration_s": result["video_s"],
              "audio_s": result["audio_s"], "encoded": result["encoded"],
              "cached": result["cached"], "srt": str(Path(result["srt"]).relative_to(project.root)),
              "vtt": str(Path(result["vtt"]).relative_to(project.root)),
              "qc": {**gate, "style_match": match, "style_detail": detail,
                     "style_overall": style_engine.overall(detail)},
              "measured": measured}
    suffix = ".vertical" if fmt == "9:16" else ""
    write_json(project.path("renders", f"edit.v{version}.{kind}{suffix}.json"), record)
    return record


def formats(brief: dict) -> list[str]:
    return {"16:9": ["16:9"], "9:16": ["9:16"], "both": ["16:9", "9:16"]}[brief["format"]]


def render_edit(project: Project, quality: str, progress: Progress = _noop,
                log: Log = _noop) -> dict:
    """Render every format the brief asks for; returns the first format's record with the
    others under `others`."""
    progress(0.02, "compiling timeline")
    version, path, _ = compile_timeline(project, log)
    meta, items = timeline.load(path)
    fmts = formats(plan.load_brief(project))
    records = []
    for n, fmt in enumerate(fmts):
        lo, hi = 0.05 + 0.9 * n / len(fmts), 0.05 + 0.9 * (n + 1) / len(fmts)
        result = render.render(project, path, quality, log=log, fmt=fmt,
                               progress=lambda f, m, lo=lo, hi=hi, fmt=fmt:
                               progress(lo + (hi - lo) * 0.85 * f, f"{fmt}: {m}"))
        progress(lo + (hi - lo) * 0.9, f"{fmt}: measuring against the style")
        records.append(_render_record(project, quality, version, result, meta, items,
                                      meta["fps"], fmt))
    progress(1.0, f"{quality} ready")
    return {**records[0], "others": records[1:]}


def _truncate(comp, seconds: float, fps: float):
    """Keep the first `seconds` of a composition; the last item is shortened, not dropped."""
    limit = round(seconds * fps)
    kept, total = [], 0
    for it in comp.items:
        if total >= limit:
            break
        take = min(it.frames, limit - total)
        if take < it.frames:
            it.frames = take
            it.src_out = it.src_in + take / fps
            it.words = [w for w in it.words if w["s"] < it.src_out]
        kept.append(it)
        total += take
    end = total / fps
    comp.items = kept
    comp.reframe = comp.reframe[: len(kept)]
    comp.zooms = [z for z in comp.zooms if z["end_s"] <= end]
    comp.broll = [b for b in comp.broll if b["end_s"] <= end]
    comp.sfx = [x for x in comp.sfx if x["t"] < end - 0.4]
    comp.beats = [{**b, "end_s": min(b["end_s"], end)} for b in comp.beats if b["start_s"] < end]
    if comp.music:
        comp.music = {**comp.music, "pieces": music.fit_plan(
            {"duration": comp.music["duration"], "downbeats": _downbeats(comp.music)}, end)}
    return comp


def _downbeats(music_doc: dict) -> list[float]:
    track = next((t for t in music.load_library() if t["id"] == music_doc["id"]), None)
    return track["downbeats"] if track else []


def try_styles(project: Project, style_ids: list[str], seconds: float = 60.0,
               progress: Progress = _noop, log: Log = _noop) -> dict:
    """Plan §5 "try both": the first `seconds` of the current edit in each candidate style,
    rendered and measured, so the creator picks by watching rather than by reading numbers."""
    if not 2 <= len(style_ids) <= 4:
        raise ValueError("try two to four styles")
    index = project.clip_index()
    brief = plan.load_brief(project)
    fps = pick_fps(index)
    library = music.load_library()
    source_colour = fx.footage_colour(project, index)
    out_dir = project.path("renders", "try")
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for n, sid in enumerate(style_ids):
        lo, hi = n / len(style_ids), (n + 1) / len(style_ids)
        progress(lo, f"{sid}: planning")
        doc = plan.trial_plan(project, sid, log=lambda m: None)
        style = styles.apply_extras(styles.get(sid), brief["extras"])
        comp = _truncate(composer.compose(index, doc, {**brief, "style": sid}, fps, library),
                         seconds, fps)
        if "target" in comp.colour:
            comp.colour = (fx.colour_transfer(comp.colour["target"], source_colour)
                           if source_colour else {})
        meta = {"name": f"try {sid}", "stem": f"try-{sid}", "version": 0, "fps": fps,
                "style": sid, "captions": comp.captions, "beats": comp.beats,
                "zooms": comp.zooms, "broll": comp.broll, "music": comp.music, "sfx": comp.sfx,
                "colour": comp.colour, "report": comp.report, "reframe": comp.reframe,
                "emphasis": {b["id"]: b.get("emphasis", []) for b in doc["beats"]},
                "source_colour": source_colour, "input_hash": f"try-{sid}",
                "music_wanted": brief["music"]["mode"] != "none" and style["music"]["presence"]}
        tl_path = out_dir / f"try-{sid}.otio"
        import opentimelineio as otio
        otio.adapters.write_to_file(timeline.build(comp.items, index, meta), str(tl_path))
        progress(lo + 0.1 * (hi - lo), f"{sid}: rendering")
        result = render.render(project, tl_path, "preview", log=log,
                               progress=lambda f, m, lo=lo, hi=hi, sid=sid: progress(
                                   lo + (0.1 + 0.7 * f) * (hi - lo), f"{sid}: {m}"))
        progress(lo + 0.85 * (hi - lo), f"{sid}: measuring")
        words = captions.output_words(comp.items, fps, meta["emphasis"])
        measured = measure.measure(Path(result["output"]), words=words)
        applied = {**style, "colour": comp.colour or {"saturation": 1.0}}
        detail = style_engine.match(measured, applied, meta["music_wanted"],
                                    comp.captions["style"] if comp.captions["enabled"] else "off",
                                    source_colour,
                                    fx.graded_colour(project, index, comp.colour or {}))
        results.append({"style": sid, "style_name": style["name"],
                        "path": str(Path(result["output"]).relative_to(project.root)),
                        "duration_s": result["video_s"], "match": detail,
                        "overall": style_engine.overall(detail)})
    write_json(out_dir / "latest.json", {"seconds": seconds, "renders": results})
    progress(1.0, "trial renders ready")
    return {"renders": results}


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
    landscape = record if record["format"] == "16:9" else None
    vertical = next((r for r in [record] + record.get("others", []) if r["format"] == "9:16"),
                    None)
    progress(0.95, "thumbnails")
    # Thumbnails are 16:9 whatever the edit's format; built from clean source frames.
    pkg["thumbnails"] = thumbnails.build(
        project, project.path((landscape or record)["path"]), words,
        pkg["titles"][0]["text"] if pkg["titles"] else (beats[0]["title"] if beats else ""),
        beats, pool, plan.load_brief(project).get("thumbnail_profile"), log,
        timeline_path=path)
    pkg["files"] = {"mp4": (landscape or record)["path"],
                    "mp4_vertical": vertical["path"] if vertical else None,
                    "srt": record["srt"], "vtt": record["vtt"],
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
