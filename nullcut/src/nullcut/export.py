"""Exports for a finishing pass in Resolve or Premiere (plan §7, Gate 3: "Resolve imports
cleanly").

Three files beside the MP4:

  edit.vN.otio     the timeline itself, media pointing at the originals; Resolve 18.5+
                   and Premiere (via the OTIO plugin) open it directly.
  edit.vN.fcpxml   FCPXML 1.8, written here rather than by the generic adapter so the
                   edit survives intact: the cut on the spine, B-roll as connected clips
                   on lane 1 (video only), the music bed's bar-line pieces and the SFX
                   on audio lanes, zooms as transform keyframes, beats as markers.
                   1.8 is the newest version every Resolve since 16 reads.
  edit.vN.cube     the grade as a 3D LUT, for a node in the colour page.

Every FCPXML is checked before it ships: references resolve, the spine is gapless and
frame-aligned, every clip stays inside its media, and the same file read back by the
independent OTIO FCPXML reader gives the same cut. A failed check is reported in the
package, never silently shipped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

from . import config, fx, timeline
from .compiler import Item
from .project import Project
from .render import fps_fraction

FCPXML_VERSION = "1.8"


# --- time ------------------------------------------------------------------------------

class Clock:
    """Rational time on a frame grid: FCPXML wants "num/dens" on frame boundaries."""

    def __init__(self, fps: float):
        self.rate = fps_fraction(fps)          # frames per second, e.g. 30000/1001
        self.frame = 1 / self.rate             # seconds per frame

    def frames(self, seconds: float) -> int:
        return round(Fraction(seconds).limit_denominator(1_000_000) * self.rate)

    def t(self, frames: int) -> str:
        v = Fraction(frames) * self.frame
        if v == 0:
            return "0s"
        return f"{v.numerator}/{v.denominator}s" if v.denominator != 1 else f"{v.numerator}s"

    def frame_duration(self) -> str:
        f = self.frame
        return f"{f.numerator}/{f.denominator}s"


def parse_time(text: str) -> Fraction:
    text = text.rstrip("s")
    if "/" in text:
        n, d = text.split("/")
        return Fraction(int(n), int(d))
    return Fraction(text)


# --- source facts ---------------------------------------------------------------------------

def _timecode_start(path: Path, fps: float) -> float:
    """A camera original's embedded start timecode, in seconds (0 when it has none).
    Resolve conforms FCPXML by source timecode, so asset times must include it."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format_tags=timecode:stream_tags=timecode", "-of", "json",
                              str(path)], capture_output=True, text=True, timeout=30).stdout
        data = json.loads(out or "{}")
        tc = (data.get("format", {}).get("tags", {}).get("timecode")
              or next((s.get("tags", {}).get("timecode") for s in data.get("streams", [])
                       if s.get("tags", {}).get("timecode")), None))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0
    if not tc:
        return 0.0
    parts = tc.replace(";", ":").split(":")
    if len(parts) != 4:
        return 0.0
    h, m, s, f = (int(p) for p in parts)
    nominal = round(fps) or 30
    return h * 3600 + m * 60 + s + f / nominal


def _audio_facts(path: Path) -> tuple[bool, int, int]:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                              "-show_entries", "stream=channels,sample_rate", "-of", "json",
                              str(path)], capture_output=True, text=True, timeout=30).stdout
        streams = json.loads(out or "{}").get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        streams = []
    if not streams:
        return False, 0, 0
    return True, int(streams[0].get("channels", 2)), int(streams[0].get("sample_rate", 48000))


def _write_wav(samples: np.ndarray, path: Path) -> None:
    pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.MIX_SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


# --- FCPXML ----------------------------------------------------------------------------------

def _zoom_keys(z: dict, start_local: Fraction, item_start_out: float, clock: Clock,
               size: tuple[int, int]) -> list[tuple[Fraction, float, float, float]]:
    """(local time, scale, x, y) keyframes for one zoom. Position is in FCPXML units, a
    percentage of frame height, y up; the window centre lands where the renderer put it."""
    w, h = size
    s = z["scale"]
    half_w, half_h = 0.5 / s, 0.5 / s
    wx = min(max(z["cx"], half_w), 1 - half_w)
    wy = min(max(z["cy"], half_h), 1 - half_h)
    x = (0.5 - wx) * w * s / h * 100
    y = (wy - 0.5) * s * 100

    def at(t_out: float) -> Fraction:
        return start_local + Fraction(clock.frames(t_out - item_start_out)) * clock.frame

    a, b, e = z["start_s"], z["end_s"], max(z["ease_s"], 1 / float(clock.rate))
    if z["kind"] == "drift":
        return [(at(a), 1.0, 0.0, 0.0), (at(b), s, x, y)]
    return [(at(a), 1.0, 0.0, 0.0), (at(a + e), s, x, y), (at(b - e), s, x, y),
            (at(b), 1.0, 0.0, 0.0)]


def _fmt_t(v: Fraction) -> str:
    return "0s" if v == 0 else (f"{v.numerator}/{v.denominator}s" if v.denominator != 1
                                else f"{v.numerator}s")


def build_fcpxml(project: Project, meta: dict, items: list[Item], name: str,
                 media_dir: Path) -> ET.ElementTree:
    fps = meta["fps"]
    clock = Clock(fps)
    size = config.MASTER_SIZE
    clips = meta["clips"]
    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    res = ET.SubElement(root, "resources")
    seq_fmt = ET.SubElement(res, "format", id="r1", name=f"FFVideoFormat{size[1]}p{fps:g}",
                            frameDuration=clock.frame_duration(), width=str(size[0]),
                            height=str(size[1]), colorSpace="1-1-1 (Rec. 709)")
    next_id = [2]
    formats: dict[tuple, str] = {}
    assets: dict[str, dict] = {}

    def new_id() -> str:
        rid = f"r{next_id[0]}"
        next_id[0] += 1
        return rid

    def asset_for(key: str, path: Path, duration_s: float, video: bool, src_fps: float,
                  width: int = 0, height: int = 0) -> dict:
        if key in assets:
            return assets[key]
        has_audio, channels, rate = _audio_facts(path)
        tc = _timecode_start(path, src_fps) if video else 0.0
        attrs = {"id": new_id(), "name": path.name, "src": path.resolve().as_uri(),
                 "start": _fmt_t(Fraction(clock.frames(tc)) * clock.frame),
                 "duration": clock.t(clock.frames(duration_s)),
                 "hasVideo": "1" if video else "0", "hasAudio": "1" if has_audio else "0"}
        if video:
            fkey = (src_fps, width, height)
            if fkey not in formats:
                fc = Clock(src_fps or fps)
                formats[fkey] = new_id()
                ET.SubElement(res, "format", id=formats[fkey], frameDuration=fc.frame_duration(),
                              width=str(width or size[0]), height=str(height or size[1]))
            attrs["format"] = formats[fkey]
        if has_audio:
            attrs.update({"audioSources": "1", "audioChannels": str(channels),
                          "audioRate": str(rate)})
        el = ET.SubElement(res, "asset", **attrs)
        assets[key] = {"id": attrs["id"], "start": parse_time(attrs["start"]), "el": el}
        return assets[key]

    lib = ET.SubElement(root, "library")
    event = ET.SubElement(lib, "event", name="nullcut")
    proj = ET.SubElement(event, "project", name=name)
    total = sum(it.frames for it in items)
    seq = ET.SubElement(proj, "sequence", format=seq_fmt.get("id"), duration=clock.t(total),
                        tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")

    spine_clips = []   # (element, out_start_s, out_end_s, local_start Fraction)
    cursor = 0
    for n, it in enumerate(items):
        c = clips[it.clip_id]
        a = asset_for(it.clip_id, Path(c["path"]), c["duration"], True, c.get("fps") or fps)
        local = a["start"] + Fraction(round(it.src_in * fps)) * clock.frame
        el = ET.SubElement(spine, "asset-clip", ref=a["id"], name=f"{c['name']} [{n:03d}]",
                           offset=clock.t(cursor), start=_fmt_t(local),
                           duration=clock.t(it.frames), tcFormat="NDF")
        spine_clips.append((el, cursor / fps, (cursor + it.frames) / fps, local))
        cursor += it.frames

    def parent_at(t_out: float):
        for sc in spine_clips:
            if sc[1] - 1e-6 <= t_out < sc[2] - 1e-6:
                return sc
        return spine_clips[-1]

    def local_time(sc, t_out: float) -> Fraction:
        return sc[3] + Fraction(clock.frames(t_out - sc[1])) * clock.frame

    # Zooms: transform keyframes on the clip they sit in (zooms never cross a cut).
    for z in meta.get("zooms", []):
        sc = parent_at(z["start_s"])
        keys = _zoom_keys(z, sc[3], sc[1], clock, size)
        tr = ET.SubElement(sc[0], "adjust-transform", position="0 0", scale="1 1")
        for pname, value in (("position", lambda k: f"{k[2]:.4f} {k[3]:.4f}"),
                             ("scale", lambda k: f"{k[1]:.4f} {k[1]:.4f}")):
            param = ET.SubElement(tr, "param", name=pname)
            anim = ET.SubElement(param, "keyframeAnimation")
            for k in keys:
                ET.SubElement(anim, "keyframe", time=_fmt_t(k[0]), value=value(k), curve="smooth")

    # B-roll on lane 1, video only: the dialogue under it keeps playing (an L-cut).
    for b in meta.get("broll", []):
        c = clips[b["clip_id"]]
        a = asset_for(b["clip_id"], Path(c["path"]), c["duration"], True, c.get("fps") or fps)
        sc = parent_at(b["start_s"])
        frames = clock.frames(b["end_s"] - b["start_s"])
        ET.SubElement(sc[0], "asset-clip", ref=a["id"], lane="1", name=f"{c['name']} B-roll",
                      offset=_fmt_t(local_time(sc, b["start_s"])),
                      start=_fmt_t(a["start"] + Fraction(round(b["src_in"] * fps)) * clock.frame),
                      duration=clock.t(frames), srcEnable="video", tcFormat="NDF")

    # Music: each bar-line piece as its own clip on lane -1, back to back.
    m = meta.get("music")
    if m and m.get("path") and Path(m["path"]).exists():
        a = asset_for(f"music:{m['path']}", Path(m["path"]), m.get("duration", total / fps),
                      False, fps)
        t = 0.0
        for k, (s0, s1) in enumerate(m["pieces"]):
            length = min(s1 - s0, total / fps - t)
            if length <= 0:
                break
            sc = parent_at(t)
            frames = clock.frames(length)
            el = ET.SubElement(sc[0], "asset-clip", ref=a["id"], lane="-1",
                               name=f"{m['name']} ({k + 1})", offset=_fmt_t(local_time(sc, t)),
                               start=_fmt_t(Fraction(clock.frames(s0)) * clock.frame),
                               duration=clock.t(frames), tcFormat="NDF")
            # The render ducks under speech; the export sits the bed at its under-speech
            # level so it never fights the dialogue before the editor shapes it.
            ET.SubElement(el, "adjust-volume", amount=f"{m.get('under_speech_db', -20):g}dB")
            t += frames / fps

    # SFX on lane -2, from the same samples the renderer mixed.
    cues = meta.get("sfx", [])
    if cues:
        media_dir.mkdir(parents=True, exist_ok=True)
        for kind in sorted({c["kind"] for c in cues}):
            path = media_dir / f"sfx_{kind}.wav"
            if not path.exists():
                _write_wav(fx.sfx_sample(kind), path)
        for cue in cues:
            path = media_dir / f"sfx_{cue['kind']}.wav"
            dur = len(fx.sfx_sample(cue["kind"])) / config.MIX_SAMPLE_RATE
            a = asset_for(f"sfx:{cue['kind']}", path, dur, False, fps)
            sc = parent_at(cue["t"])
            frames = max(1, min(clock.frames(dur), clock.frames(total / fps - cue["t"])))
            el = ET.SubElement(sc[0], "asset-clip", ref=a["id"], lane="-2",
                               name=f"SFX {cue['kind']}", offset=_fmt_t(local_time(sc, cue["t"])),
                               start="0s", duration=clock.t(frames), tcFormat="NDF")
            ET.SubElement(el, "adjust-volume", amount="-10dB")

    # Beats as markers: the storyboard's structure survives the round trip.
    for beat in meta.get("beats", []):
        sc = parent_at(beat["start_s"])
        ET.SubElement(sc[0], "marker", start=_fmt_t(local_time(sc, beat["start_s"])),
                      duration=clock.frame_duration(), value=f"{beat['role']}: {beat['title']}")

    # FCPXML wants children in DTD order: transform and volume before connected clips
    # and markers. Sort each spine clip's children by that order.
    rank = {"adjust-transform": 0, "adjust-volume": 1, "marker": 3, "asset-clip": 2}
    for sc in spine_clips:
        kids = list(sc[0])
        for k in kids:
            sc[0].remove(k)
        for k in sorted(kids, key=lambda e: rank.get(e.tag, 9)):
            sc[0].append(k)
    for c in res.findall("asset"):  # resources in DTD order: formats, then assets
        res.remove(c)
        res.append(c)
    ET.indent(root)
    return ET.ElementTree(root)


def write_fcpxml(project: Project, timeline_path: Path, out: Path) -> Path:
    meta, items = timeline.load(timeline_path)
    tree = build_fcpxml(project, meta, items, f"{project.manifest()['name']} v{meta['version']}",
                        out.parent / "media")
    tmp = out.with_name(f".{out.name}")
    with tmp.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    tmp.replace(out)
    return out


# --- checks ------------------------------------------------------------------------------------

def check_fcpxml(path: Path, timeline_path: Path) -> dict:
    """Structural checks of what Resolve's importer needs, plus a read-back through the
    independent OTIO FCPXML reader. Returns {"ok", "errors", "warnings", ...}."""
    errors, warnings = [], []
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != "fcpxml" or root.get("version") != FCPXML_VERSION:
        errors.append(f"root must be fcpxml version {FCPXML_VERSION}")
    res = {e.get("id"): e for e in root.find("resources")}
    if len(res) != len(root.find("resources")):
        errors.append("duplicate resource ids")
    seq = root.find("library/event/project/sequence")
    if seq is None:
        return {"ok": False, "errors": ["no sequence"], "warnings": []}
    fd = parse_time(res[seq.get("format")].get("frameDuration"))

    def on_grid(v: Fraction) -> bool:
        return (v / fd).denominator == 1

    spine = seq.find("spine")
    cursor = Fraction(0)
    n_conn = 0
    for clip in spine:
        off, dur = parse_time(clip.get("offset")), parse_time(clip.get("duration"))
        start = parse_time(clip.get("start", "0s"))
        if off != cursor:
            errors.append(f"{clip.get('name')}: offset {clip.get('offset')} leaves a gap or "
                          "overlap on the spine")
        if not (on_grid(off) and on_grid(dur)):
            errors.append(f"{clip.get('name')}: offset or duration off the frame grid")
        cursor = off + dur
        ref = res.get(clip.get("ref"))
        if ref is None or ref.tag != "asset":
            errors.append(f"{clip.get('name')}: ref {clip.get('ref')} does not resolve")
            continue
        a_start, a_dur = parse_time(ref.get("start")), parse_time(ref.get("duration"))
        if start < a_start or start + dur > a_start + a_dur + fd:
            errors.append(f"{clip.get('name')}: source range outside its media")
        for child in clip:
            if child.tag == "asset-clip":
                n_conn += 1
                c_off = parse_time(child.get("offset"))
                if not start <= c_off < start + dur:
                    errors.append(f"{child.get('name')}: connected clip anchored outside "
                                  f"its parent {clip.get('name')}")
                if child.get("lane") in (None, "0"):
                    errors.append(f"{child.get('name')}: connected clip without a lane")
                cref = res.get(child.get("ref"))
                if cref is None:
                    errors.append(f"{child.get('name')}: ref does not resolve")
                else:
                    cs, cd = parse_time(child.get("start")), parse_time(child.get("duration"))
                    if cs < parse_time(cref.get("start")) or cs + cd > \
                            parse_time(cref.get("start")) + parse_time(cref.get("duration")) + fd:
                        errors.append(f"{child.get('name')}: source range outside its media")
            elif child.tag == "marker":
                ms = parse_time(child.get("start"))
                if not start <= ms < start + dur:
                    errors.append(f"marker {child.get('value')!r} outside its clip")
            elif child.tag == "adjust-transform":
                for kf in child.iter("keyframe"):
                    kt = parse_time(kf.get("time"))
                    if not start <= kt <= start + dur:
                        errors.append(f"{clip.get('name')}: zoom keyframe outside the clip")
    if cursor != parse_time(seq.get("duration")):
        errors.append("spine length differs from the sequence duration")
    for a in root.iter("asset"):
        src = a.get("src", "")
        if not src.startswith("file://"):
            errors.append(f"asset {a.get('name')}: src is not a file URL")
        elif not Path(unquote(urlparse(src).path)).exists():
            warnings.append(f"asset {a.get('name')}: media not found at {src}")

    # The same cut, read back by an independent implementation.
    meta, items = timeline.load(timeline_path)
    fps = meta["fps"]
    roundtrip = {"read": False}
    try:
        import opentimelineio as otio
        tl = otio.adapters.read_from_file(str(path), adapter_name="fcpx_xml")
        if not isinstance(tl, otio.schema.Timeline):  # a library reads as a collection
            tl = next(c for c in tl.find_children(descended_from_type=otio.schema.Timeline))
        # The reader names tracks by lane; lane 0 is the spine.
        by_lane = {t.name: t for t in tl.tracks}
        got = [round(c.source_range.duration.to_frames()) for c in by_lane["0"].find_clips()]
        want = [it.frames for it in items]
        broll = len(by_lane["1"].find_clips()) if "1" in by_lane else 0
        roundtrip = {"read": True, "clips": len(got), "expected": len(want),
                     "durations_match": got == want, "lanes": sorted(by_lane),
                     "broll": broll, "broll_expected": len(meta.get("broll", []))}
        if got != want:
            errors.append(f"read back {len(got)} cut clips with different lengths than the "
                          f"{len(want)} in the timeline")
        if broll != len(meta.get("broll", [])):
            errors.append(f"read back {broll} B-roll clips, the timeline has "
                          f"{len(meta.get('broll', []))}")
    except Exception as exc:  # the reader is a check; its absence is not an export error
        warnings.append(f"OTIO FCPXML reader could not read it back: {exc}")
    return {"ok": not errors, "version": FCPXML_VERSION, "errors": errors[:20],
            "warnings": warnings[:20],
            "spine_clips": len(spine), "connected_clips": n_conn,
            "duration_s": round(float(cursor), 3), "fps": fps, "roundtrip": roundtrip}


# --- package ---------------------------------------------------------------------------------

def write_all(project: Project, timeline_path: Path, out_dir: Path, version: int,
              srt: Path | None = None) -> dict:
    """OTIO, FCPXML and LUT into the package; returns relative paths and the checks."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rel = lambda p: str(p.relative_to(project.root))  # noqa: E731
    files: dict[str, str | None] = {}
    otio_out = out_dir / f"edit.v{version}.otio"
    shutil.copyfile(timeline_path, otio_out)
    files["otio"] = rel(otio_out)
    fcp = out_dir / f"edit.v{version}.fcpxml"
    checks: dict = {}
    try:
        write_fcpxml(project, timeline_path, fcp)
        files["fcpxml"] = rel(fcp)
        checks["fcpxml"] = check_fcpxml(fcp, timeline_path)
    except Exception as exc:  # an export problem must not cost the creator their master
        files["fcpxml"] = None
        checks["fcpxml"] = {"ok": False, "errors": [f"export failed: {exc}"], "warnings": []}
    meta, _ = timeline.load(timeline_path)
    colour = meta.get("colour") or {}
    if colour and not fx.is_identity(colour):
        cube = out_dir / f"edit.v{version}.cube"
        cube.unlink(missing_ok=True)
        fx.write_cube(cube, colour)
        files["cube"] = rel(cube)
    else:
        files["cube"] = None
    if srt is not None and srt.exists():
        shutil.copyfile(srt, out_dir / f"edit.v{version}.srt")
    return {"files": files, "checks": checks}
