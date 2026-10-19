"""nullcut command line. Phase 0: ingest, rough, render, info."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__, analyse, asr, captions, config, models, project as proj, render, timeline
from .compiler import CutOptions, compile_rough, pick_fps
from .project import read_json, stable_hash



def parse_duration(text: str) -> float:
    """'8m', '90s', '1h2m', '4:00', '1:02:03' or plain seconds."""
    t = text.strip().lower()
    if ":" in t:
        parts = [float(p) for p in t.split(":")]
        secs = 0.0
        for p in parts:
            secs = secs * 60 + p
        return secs
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?", t)
    if not t or not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"cannot read duration {text!r}")
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def parse_size(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)x(\d+)", text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"size must look like 1920x1080, got {text!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if w % 2 or h % 2:
        raise argparse.ArgumentTypeError("H.264 needs even width and height")
    return w, h


def mmss(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    m, s = divmod(seconds, 60)
    return f"{sign}{int(m)}:{s:04.1f}"


def cmd_ingest(args: argparse.Namespace) -> int:
    home = config.nullcut_home(args.home)
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        raise SystemExit(f"{folder} is not a folder")
    project = proj.open_or_create(home, folder, args.project)
    print(f"project {project.id}  ({project.root})")
    index = analyse.ingest(project, folder, args.asr, args.language,
                           Path(args.transcripts) if args.transcripts else None, args.recursive)
    words = sum(len(c["words"]) for c in index["clips"])
    total = sum(c["duration"] for c in index["clips"])
    print(f"indexed {len(index['clips'])} clips, {mmss(total)} of footage, {words} words")
    return 0


def print_cut(report: dict) -> None:
    rows = [
        ("source", f"{mmss(report['source_s'])} across {report['clips']} clips"),
        ("kept", f"{mmss(report['kept_s'])} in {report['items']} items, "
                 f"{report['jump_cuts']} jump cuts"),
        ("dead air", f"-{mmss(report['dead_air_s'])} at silence threshold "
                     f"{report['silence_threshold_s'] * 1000:.0f} ms"
                     + (" (tightened)" if report["tightened_to"] else "")),
        ("fillers", f"-{mmss(report['filler_s'])} ({report['fillers']} words)"),
    ]
    if report["dropped_segments"]:
        rows.append(("dropped", f"-{mmss(report['dropped_s'])} "
                                f"({len(report['dropped_segments'])} sentences, "
                                f"{report.get('solver') or 'greedy'} fit on highlight score)"))
    if report.get("target_s"):
        state = "met" if report["target_met"] else "MISSED"
        rows.append(("target", f"{mmss(report['target_s'])} ±{report['tolerance']:.0%}: {state}"))
    if report["skipped_no_speech"]:
        rows.append(("skipped", ", ".join(report["skipped_no_speech"]) + " (no speech)"))
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")
    if report.get("shortfall_s"):
        print(f"\nshort by {report['shortfall_s']:.1f} s against the target window. "
              "Not padded: shoot more, lower the target, or loosen --silence-ms.")
    if report.get("overshoot_s"):
        print(f"\nover by {report['overshoot_s']:.1f} s: no sentence could be dropped "
              "without undershooting further.")


def cmd_rough(args: argparse.Namespace) -> int:
    home = config.nullcut_home(args.home)
    if args.project and Path(args.project).expanduser().is_dir():
        folder = Path(args.project).expanduser()
        try:
            project = proj.resolve(home, args.project)
        except SystemExit:
            # One command from folder to edit: ingest on first sight.
            project = proj.open_or_create(home, folder)
            analyse.ingest(project, folder)
        else:
            if not project.clip_index_path.exists():
                analyse.ingest(project, folder)
    else:
        project = proj.resolve(home, args.project)

    index = project.clip_index()
    fps = args.fps or pick_fps(index)
    opts = CutOptions(
        target_s=args.target, tolerance=args.tolerance,
        silence_s=(args.silence_ms / 1000) if args.silence_ms else config.SILENCE_THRESHOLD_S,
        fps=fps, remove_fillers=not args.keep_fillers,
    )
    result = compile_rough(index, opts)
    if not result.items:
        raise SystemExit("nothing to cut: no speech found in any clip")

    cap = {"enabled": args.captions, "style": args.caption_style}
    meta = {
        "name": f"{read_json(project.manifest_path)['name']} rough",
        "fps": fps, "captions": cap, "report": result.report,
        "input_hash": stable_hash({"index": read_json(project.clip_index_path),
                                   "opts": vars(opts), "captions": cap}),
    }
    version, path, created = timeline.save(project, result.items, index, meta)
    print(f"timeline v{version}{'' if created else ' (unchanged, reused)'}: {path}")
    print_cut(result.report)

    if args.no_render:
        return 0
    quality = "preview" if args.preview else "master"
    out = render.render(project, path, quality, args.size, args.jobs)
    print_render(out)
    return 0


def print_render(out: dict) -> None:
    drift = abs(out["video_s"] - out["audio_s"])
    lufs = out.get("integrated_lufs")
    tp = out.get("true_peak_dbtp")
    print(f"\n  video     {out['output']}")
    print(f"  timing    {mmss(out['video_s'])} video, {mmss(out['audio_s'])} audio, "
          f"drift {drift * 1000:.0f} ms")
    print(f"  loudness  {lufs if lufs is not None else '-inf'} LUFS integrated, "
          f"{tp if tp is not None else '-inf'} dBTP peak")
    print(f"  encode    {out['encoded']} items encoded, {out['cached']} reused from cache")
    print(f"  captions  {out['srt']}")


def cmd_render(args: argparse.Namespace) -> int:
    home = config.nullcut_home(args.home)
    project = proj.resolve(home, args.project)
    if args.timeline:
        path = project.path("timelines", f"timeline.v{args.timeline}.otio")
        if not path.exists():
            raise SystemExit(f"no timeline v{args.timeline} in {project.id}")
    else:
        latest = project.latest("timelines", "timeline", ".otio")
        if not latest:
            raise SystemExit("no timeline yet: run `nullcut rough` first")
        path = latest[1]
    out = render.render(project, path, "preview" if args.preview else "master",
                        args.size, args.jobs)
    print_render(out)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    home = config.nullcut_home(args.home)
    project = proj.resolve(home, args.project)
    manifest = project.manifest()
    print(f"project {project.id}\n  source  {manifest['source_dir']}\n  root    {project.root}")
    if project.clip_index_path.exists():
        for c in project.clip_index()["clips"]:
            print(f"  {c['id']}  {c['name']:<32} {mmss(c['duration']):>8}  "
                  f"{len(c['words']):>5} words  {c['language']}  {c['asr']}")
    for p in sorted(project.path("timelines").glob("timeline.v*.otio"),
                    key=lambda p: proj.natural_key(p.name)):
        meta, items = timeline.load(p)
        r = meta.get("report", {})
        print(f"  {p.name:<20} {mmss(r.get('kept_s')):>8}  {len(items)} items")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    if args.action == "pull":
        if not args.name or args.name == "defaults":
            for name in models.DEFAULT_PROFILE.values():
                models.pull(name)
            return 0
        if args.name not in models.REGISTRY:
            raise SystemExit(f"unknown model {args.name!r}; see `nullcut models`")
        models.pull(args.name)
        return 0
    default = set(models.DEFAULT_PROFILE.values())
    print(f"models dir  {models.models_dir()}")
    for m in models.REGISTRY.values():
        state = ("installed" if models.installed(m.name)
                 else "on first use" if m.url is None else "not pulled")
        flag = "default" if m.name in default else ""
        print(f"  {m.name:<22} {m.job:<4} {m.licence:<10} {state:<12} {flag}")
        print(f"  {'':<22} {m.notes}")
    return 0


def _progress(frac: float, msg: str) -> None:
    sys.stderr.write(f"\r  {frac:4.0%}  {msg[:70]:<70}")
    sys.stderr.flush()


def cmd_brief(args: argparse.Namespace) -> int:
    from . import plan

    project = proj.resolve(config.nullcut_home(args.home), args.project)
    updates: dict = {}
    if args.text is not None:
        updates["text"] = args.text
    if args.target is not None:
        updates["target_s"] = args.target
    if args.style:
        updates["style"] = args.style
    if args.captions:
        updates["captions"] = {"mode": args.captions}
    if args.music:
        updates["music"] = {"mode": "none" if args.music == "none" else "library",
                            "mood": "" if args.music in ("none", "library") else args.music}
    brief = plan.save_brief(project, updates) if updates else plan.load_brief(project)
    print(json.dumps(brief, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from . import pipeline

    project = proj.resolve(config.nullcut_home(args.home), args.project)
    answers = {}
    if args.answer:
        from . import plan
        current = plan.latest_plan(project) or {}
        for q, a in zip(current.get("questions", []), args.answer):
            answers[q["text"]] = a
    doc = pipeline.make_plan(project, answers, log=lambda m: print(f"  {m}"))
    if doc["questions"]:
        print("\nThe planner needs an answer before it can plan:")
        for q in doc["questions"]:
            print(f"  {q['text']}")
        print("Re-run with --answer \"...\" (one per question, in order).")
        return 0
    s = doc["solver"]
    print(f"plan v{doc['version']} by {doc['planner']}, style {doc['style']}: "
          f"{mmss(s['kept_s'])} kept, {s['status']}")
    for b in doc["beats"]:
        lock = " [locked]" if b.get("locked") else ""
        print(f"  {b['id']:<4} {b['role']:<7} {mmss(b['duration_s']):>7}  {b['title']}{lock}")
    return 0


def cmd_edit_render(args: argparse.Namespace) -> int:
    from . import pipeline

    project = proj.resolve(config.nullcut_home(args.home), args.project)
    if args.command == "master":
        out = pipeline.master(project, _progress)
        rec, pkg = out["render"], out["package"]
    else:
        rec, pkg = pipeline.render_edit(project, "preview", _progress), None
    sys.stderr.write("\n")
    qc = rec["qc"]
    print(f"  video     {project.path(rec['path'])}")
    print(f"  length    {mmss(rec['duration_s'])}  ok: {qc['length_ok']}")
    print(f"  loudness  {qc['loudness_lufs']} LUFS, {qc['true_peak_dbtp']} dBTP")
    print(f"  cuts      clean: {qc['cuts_clean']} (worst {qc['worst_cut_db']} dB under speech)")
    print("  style     " + ", ".join(f"{k} {v:.0%}" for k, v in qc["style_match"].items()))
    if pkg:
        print("  titles")
        for t in pkg["titles"]:
            print(f"    {t['text']}")
        for t in pkg.get("rejected_titles", []):
            print(f"    (dropped, not in the transcript) {t['text']}")
        print(f"  package   {project.path('package')}")
    return 0


def cmd_music(args: argparse.Namespace) -> int:
    from . import music

    if args.action == "import":
        if not args.folder:
            raise SystemExit("which folder? `nullcut music import <folder>`")
        music.import_folder(Path(args.folder).expanduser())
        return 0
    for t in music.load_library():
        print(f"  {t['id']}  {t['name']:<32} {t['bpm']:>5.0f} bpm  energy {t['energy']:.2f}  "
              f"{t['licence']}")
    return 0


def cmd_styles(args: argparse.Namespace) -> int:
    from . import style_engine, styles

    if args.action == "extract":
        if not (args.name and args.folder):
            raise SystemExit("usage: nullcut styles extract NAME FOLDER")
        prof = style_engine.extract(args.name, Path(args.folder).expanduser())
        print(f"style {prof['id']} from {len(prof['sources'])} references "
              f"(closest shipped: {prof['inherited_from']})")
        return 0
    if args.action == "compare":
        ids = [args.name] + ([args.folder] if args.folder else [])
        for row in style_engine.compare(ids):
            vals = "  ".join(f"{k}={'-' if v is None else v}" for k, v in row["values"].items())
            print(f"  {row['label']:<20} {vals}  ({row['unit']})")
        return 0
    for st in styles.all_styles().values():
        print(f"  {st['id']:<18} {st.get('kind', 'shipped'):<9} {st['description'][:70]}")
    return 0


def cmd_try(args: argparse.Namespace) -> int:
    from . import pipeline

    project = proj.resolve(config.nullcut_home(args.home), args.project)
    out = pipeline.try_styles(project, args.styles, args.seconds, _progress)
    sys.stderr.write("\n")
    for r in out["renders"]:
        weak = ", ".join(f"{k} {d['score']:.0%}" for k, d in r["match"].items() if d["note"])
        print(f"  {r['style']:<16} {r['overall']:.0%}  {project.path(r['path'])}"
              + (f"  (under 80 %: {weak})" if weak else ""))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server

    print(f"nullcut on http://{args.host}:{args.port}")
    server.serve(args.host, args.port, args.home)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nullcut", description=__doc__)
    p.add_argument("--version", action="version", version=f"nullcut {__version__}")
    p.add_argument("--home", help="data root (default $NULLCUT_HOME or ~/nullcut)")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("ingest", help="proxies, audio and Clip Index for a folder of clips")
    i.add_argument("folder")
    i.add_argument("--project", help="project name (default: folder name)")
    i.add_argument("--asr", choices=asr.BACKENDS, default="auto")
    i.add_argument("--language", help="force ASR language, e.g. en or nl")
    i.add_argument("--transcripts", help="folder of <clip>.words.json sidecars")
    i.add_argument("--recursive", action="store_true")
    i.set_defaults(func=cmd_ingest)

    r = sub.add_parser("rough", help="jump-cut rough edit to an OTIO timeline and an MP4")
    r.add_argument("project", nargs="?", help="project id or source folder (default: last used)")
    r.add_argument("--target", type=parse_duration, help="target length, e.g. 8m or 4:30")
    r.add_argument("--tolerance", type=float, default=config.TARGET_TOLERANCE)
    r.add_argument("--silence-ms", type=float, help="pause length that triggers a jump cut")
    r.add_argument("--keep-fillers", action="store_true")
    r.add_argument("--captions", action="store_true", help="burn in captions")
    r.add_argument("--caption-style", choices=captions.STYLES, default="clean")
    r.add_argument("--fps", type=float, help="output frame rate (default: most common source rate)")
    r.add_argument("--preview", action="store_true", help="render 720p from proxies")
    r.add_argument("--size", type=parse_size, help="output size, e.g. 1920x1080")
    r.add_argument("--jobs", type=int, help="parallel item encodes")
    r.add_argument("--no-render", action="store_true", help="write the timeline only")
    r.set_defaults(func=cmd_rough)

    d = sub.add_parser("render", help="render a timeline version")
    d.add_argument("project", nargs="?")
    d.add_argument("--timeline", type=int, help="timeline version (default: latest)")
    d.add_argument("--preview", action="store_true")
    d.add_argument("--size", type=parse_size)
    d.add_argument("--jobs", type=int)
    d.set_defaults(func=cmd_render)

    b = sub.add_parser("brief", help="show or update the brief")
    b.add_argument("project", nargs="?")
    b.add_argument("--text")
    b.add_argument("--target", type=parse_duration)
    b.add_argument("--style", choices=sorted(__import__("nullcut.styles", fromlist=["x"])
                                             .all_styles()))
    b.add_argument("--captions", choices=("off", "clean", "word"))
    b.add_argument("--music", help="none, library, or a mood such as 'calm piano'")
    b.set_defaults(func=cmd_brief)

    pl = sub.add_parser("plan", help="plan the edit against the brief and style")
    pl.add_argument("project", nargs="?")
    pl.add_argument("--answer", action="append", help="answer to the planner's question")
    pl.set_defaults(func=cmd_plan)

    for name, desc in (("preview", "720p preview of the planned edit"),
                       ("master", "full-resolution master plus titles, chapters, exports")):
        r_ = sub.add_parser(name, help=desc)
        r_.add_argument("project", nargs="?")
        r_.set_defaults(func=cmd_edit_render)

    mu = sub.add_parser("music", help="the music library")
    mu.add_argument("action", nargs="?", choices=("list", "import"), default="list")
    mu.add_argument("folder", nargs="?")
    mu.set_defaults(func=cmd_music)

    st = sub.add_parser("styles", help="list, extract or compare style profiles")
    st.add_argument("action", nargs="?", choices=("list", "extract", "compare"), default="list")
    st.add_argument("name", nargs="?", help="extract: profile name; compare: first style id")
    st.add_argument("folder", nargs="?", help="extract: reference folder; compare: second id")
    st.set_defaults(func=cmd_styles)

    tr = sub.add_parser("try", help="first 60 s of the edit in 2-4 styles, measured")
    tr.add_argument("styles", nargs="+")
    tr.add_argument("--project")
    tr.add_argument("--seconds", type=float, default=60.0)
    tr.set_defaults(func=cmd_try)

    sv = sub.add_parser("serve", help="the local web UI and API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=cmd_serve)

    m = sub.add_parser("models", help="list model weights, or pull one")
    m.add_argument("action", nargs="?", choices=("list", "pull"), default="list")
    m.add_argument("name", nargs="?")
    m.set_defaults(func=cmd_models)

    n = sub.add_parser("info", help="clips and timelines in a project")
    n.add_argument("project", nargs="?")
    n.set_defaults(func=cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except render.media.FFmpegError as exc:
        print(f"nullcut: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
