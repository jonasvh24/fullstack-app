"""The seven-step flow of plan §2 as callable steps, shared by the CLI and the HTTP server.

Each step reads the previous step's versioned file and writes its own, so any
step can be re-run alone: new music re-runs compile and render, a new brief
re-runs plan onwards, analysis never re-runs for a clip it has seen.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from . import (analyse, captions, composer, export, fx, measure, music, package, plan, qc,
               refine, render, style_engine, styles, thumbnails, timeline)
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
    # The previous timeline's decisions are kept where they still fit (composer docstring).
    latest = project.latest("timelines", "timeline", ".otio")
    prior = timeline.load(latest[1])[0] if latest else None
    comp = composer.compose(index, doc, brief, fps, library, prior)
    style = styles.for_brief(doc["style"], brief)
    source_colour = fx.footage_colour(project, index)
    colour_capped = False
    if "target" in comp.colour:
        # An extracted profile stores a look, not a tweak: fit the LUT to this footage.
        target = comp.colour["target"]
        comp.colour = fx.colour_transfer(target, source_colour) if source_colour else {}
        reach = style_engine.expected_colour(comp.colour, source_colour) if comp.colour else None
        colour_capped = bool(reach) and abs(reach["warmth"] - target["warmth"]) > 0.05
    comp.colour = fx.nudge(comp.colour, brief.get("overrides", {}).get("colour_nudge"))
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


def _record_path(project: Project, kind: str, version: int, fmt: str) -> Path:
    suffix = ".vertical" if fmt == "9:16" else ""
    return project.path("renders", f"edit.v{version}.{kind}{suffix}.json")


def _base_record(project: Project, kind: str, version: int, result: dict, meta: dict,
                 fmt: str) -> dict:
    """What a render can say the moment it exists: files, Gate 1, cache use. The style
    measurement follows (measure_record), so a refinement's preview plays first."""
    gate = qc.gate1(result, meta.get("target_s"), meta.get("tolerance", 0.05))
    record = {"kind": kind, "format": fmt, "version": version,
              "path": str(Path(result["output"]).relative_to(project.root)),
              "duration_s": result["video_s"], "audio_s": result["audio_s"],
              "encoded": result["encoded"], "cached": result["cached"],
              "audio_cached": result.get("audio_cached", False),
              "srt": str(Path(result["srt"]).relative_to(project.root)),
              "vtt": str(Path(result["vtt"]).relative_to(project.root)),
              "qc": {**gate, "style_match": {}, "style_detail": {}, "style_overall": None},
              "qc_pending": True, "measured": None}
    write_json(_record_path(project, kind, version, fmt), record)
    return record


def measure_record(project: Project, record: dict) -> dict:
    """Plan §5: the same measurements that build a profile run on the output."""
    path = project.path("timelines", f"timeline.v{record['version']}.otio")
    meta, items = timeline.load(path)
    detail, measured = _style_detail(project, record, meta, items, meta["fps"])
    record = {**record, "qc": {**record["qc"], "style_match": {k: d["score"]
                                                               for k, d in detail.items()},
                               "style_detail": detail,
                               "style_overall": style_engine.overall(detail)},
              "measured": measured, "qc_pending": False}
    write_json(_record_path(project, record["kind"], record["version"], record["format"]),
               record)
    return record


def _style_detail(project: Project, record: dict, meta: dict, items, fps: float):
    style = styles.for_brief(meta["style"], plan.load_brief(project))
    words = captions.output_words(items, fps, meta.get("emphasis"))
    cap = meta["captions"]
    result = {"output": str(project.path(record["path"]))}
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
    return detail, measured


def formats(brief: dict) -> list[str]:
    return {"16:9": ["16:9"], "9:16": ["9:16"], "both": ["16:9", "9:16"]}[brief["format"]]


def render_edit(project: Project, quality: str, progress: Progress = _noop,
                log: Log = _noop, measure_now: bool = True) -> dict:
    """Render every format the brief asks for; returns the first format's record with the
    others under `others`. With measure_now=False the style measurement is left to
    measure_record, so the caller can show the preview first."""
    progress(0.02, "compiling timeline")
    version, path, _ = compile_timeline(project, log)
    fmts = formats(plan.load_brief(project))
    span = 0.9 if measure_now else 0.95
    records = []
    for n, fmt in enumerate(fmts):
        lo, hi = 0.05 + span * n / len(fmts), 0.05 + span * (n + 1) / len(fmts)
        result = render.render(project, path, quality, log=log, fmt=fmt,
                               progress=lambda f, m, lo=lo, hi=hi, fmt=fmt:
                               progress(lo + (hi - lo) * (0.85 if measure_now else 1.0) * f,
                                        f"{fmt}: {m}"))
        rec = _base_record(project, quality, version, result, timeline.load(path)[0], fmt)
        if measure_now:
            progress(lo + (hi - lo) * 0.9, f"{fmt}: measuring against the style")
            rec = measure_record(project, rec)
        records.append(rec)
    progress(1.0, f"{quality} ready")
    return {**records[0], "others": records[1:]}


def refine_edit(project: Project, instruction: str, progress: Progress = _noop,
                log: Log = _noop, use_llm: bool = True,
                on_preview: Callable[[dict], None] | None = None) -> dict:
    """Plan §2 step 6: one chat instruction to an updated preview, timed. Returns the
    refinement step; the preview is published through on_preview before QC runs."""
    t0 = time.perf_counter()
    progress(0.02, "reading the instruction")
    interp = refine.interpret(project, instruction, use_llm=use_llm, log=log)
    if interp["kind"] == "unclear":
        return {"step": None, "interpretation": interp}
    t1 = time.perf_counter()
    progress(0.08, f"{interp['kind']}: {interp.get('summary', '')}"[:200])
    step = refine.apply(project, interp, instruction, log)
    t2 = time.perf_counter()
    record = render_edit(project, "preview", lambda f, m: progress(0.1 + 0.75 * f, m), log,
                         measure_now=False)
    t3 = time.perf_counter()
    recs = [record] + record.get("others", [])
    step["render"] = {"version": record["version"],
                      "items": sum(r["encoded"] + r["cached"] for r in recs),
                      "encoded": sum(r["encoded"] for r in recs),
                      "cached": sum(r["cached"] for r in recs),
                      "audio_remixed": not all(r["audio_cached"] for r in recs)}
    step["timing"] = {"interpret_s": round(t1 - t0, 2), "apply_s": round(t2 - t1, 2),
                      "render_s": round(t3 - t2, 2), "to_preview_s": round(t3 - t0, 2)}
    refine.update_step(project, step)
    if on_preview:
        on_preview({"step": step, "render": record})
    progress(0.88, f"preview ready in {t3 - t0:.1f} s; measuring against the style")
    measured = [measure_record(project, r) for r in recs]
    step["timing"]["qc_s"] = round(time.perf_counter() - t3, 2)
    refine.update_step(project, step)
    progress(1.0, "refined")
    return {"step": step, "render": {**measured[0], "others": measured[1:]}}


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
    # Trials compare styles as shipped (plus sliders), never this edit's refinements.
    brief = {**plan.load_brief(project), "overrides": {}}
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
        style = styles.for_brief(sid, brief)
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
    progress(0.97, "exports for Resolve and Premiere")
    exported = export.write_all(project, path, out, version,
                                srt=project.path(record["srt"]))
    pkg["files"] = {"mp4": (landscape or record)["path"],
                    "mp4_vertical": vertical["path"] if vertical else None,
                    "srt": record["srt"], "vtt": record["vtt"], **exported["files"]}
    pkg["export_checks"] = exported["checks"]
    pkg["version"] = version
    write_json(out / "package.json", pkg)
    (out / "description.md").write_text(pkg["description"] + "\n")
    write_json(out / "titles.json", pkg["titles"])
    progress(1.0, "master and package ready")
    return {"render": record, "package": pkg}

