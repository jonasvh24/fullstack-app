"""nullcut command line. Phase 0: ingest, rough, render, info."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from . import __version__, analyse, asr, captions, config, project as proj, render, timeline
from .compiler import CutOptions, compile_rough
from .project import read_json, stable_hash

STANDARD_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)


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


def pick_fps(index: dict) -> float:
    rates = [c["fps"] for c in index["clips"] if c["words"] and c["fps"]]
    if not rates:
        return config.DEFAULT_FPS
    common = Counter(round(r, 2) for r in rates).most_common(1)[0][0]
    # Phone VFR averages land on 29.83 and similar; snap to the rate a viewer expects.
    return min(STANDARD_RATES, key=lambda r: abs(r - common))


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
                                f"({len(report['dropped_segments'])} lowest-scoring sentences)"))
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
