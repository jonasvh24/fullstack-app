"""Render an OTIO timeline: video per item, audio once (plan §7).

Every item renders to its own file with identical encoder settings, keyed by
a hash of everything that affects its pixels, and the files join by stream
copy. Audio is assembled once at sample precision and loudness-normalised.
Changing one item's captions re-renders that item and the concat; nothing else.
"""

from __future__ import annotations

import json
import os
import re
import wave
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np

from . import captions, config, media, timeline
from .compiler import Item, contiguous
from .project import Project, read_json, stable_hash, write_json

# Bump when render output changes for identical inputs; old cache entries
# simply stop matching.
RENDER_VERSION = 3

Log = Callable[[str], None]


NTSC = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001))


def fps_fraction(fps: float) -> Fraction:
    # "29.97" written by a person or a probe means 30000/1001, never 2997/100.
    for rate in NTSC:
        if abs(fps - float(rate)) < 0.005:
            return rate
    return Fraction(fps).limit_denominator(1001)


def track_timescale(fps: float) -> int:
    f = fps_fraction(fps)
    # 90 kHz when a frame is a whole number of ticks; the rate's own
    # numerator otherwise (23.976 does not divide 90 kHz).
    return 90000 if (90000 * f.denominator) % f.numerator == 0 else f.numerator


def _filter_path(path: Path) -> str:
    # Filter-graph arguments treat ':' '\' and quotes as syntax.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _concat_line(path: Path) -> str:
    # The concat demuxer quotes like a POSIX shell: close, escaped quote, reopen.
    return "file '" + str(path).replace("'", "'\\''") + "'\n"


def _smooth(u: str) -> str:
    """Smoothstep of an expression already clipped to 0..1, via a register."""
    return f"(st(9,{u})*ld(9)*(3-2*ld(9)))"


def zoom_filter(zooms: list[dict], fps: float, interpolation: str = "cubic") -> str:
    """Sub-pixel crop-and-scale through `perspective` (plan §7: not zoompan, which steps on
    whole pixels and jitters on slow moves).

    Each zoom contributes an eased envelope; scale and centre are blended by it,
    and the window is clamped inside the frame so a zoom never shows a border.
    """
    T = f"(in/{fps:.6f})"
    envs = []
    for z in zooms:
        a, b, e = z["start"], z["end"], max(z["ease_s"], 1e-3)
        if z["kind"] == "drift":
            env = _smooth(f"clip(({T}-{a:.4f})/{b - a:.4f},0,1)")
        else:
            up = _smooth(f"clip(({T}-{a:.4f})/{e:.4f},0,1)")
            down = _smooth(f"clip(({T}-{b - e:.4f})/{e:.4f},0,1)")
            env = f"({up}*(1-{down}))"
        envs.append((env, z))
    zexpr = "1" + "".join(f"+{z['scale'] - 1:.4f}*{env}" for env, z in envs)
    cx = "0.5" + "".join(f"+({z['cx'] - 0.5:.4f})*{env}" for env, z in envs)
    cy = "0.5" + "".join(f"+({z['cy'] - 0.5:.4f})*{env}" for env, z in envs)
    # Registers: 1 = zoom, 2 = half width, 3 = half height, 4 = left, 5 = top.
    pre = (f"st(1,{zexpr})+st(2,W/(2*ld(1)))+st(3,H/(2*ld(1)))"
           f"+st(4,clip(W*({cx})-ld(2),0,W-2*ld(2)))+st(5,clip(H*({cy})-ld(3),0,H-2*ld(3)))")
    corner = {
        "x0": "ld(4)", "y0": "ld(5)",
        "x1": "ld(4)+2*ld(2)", "y1": "ld(5)",
        "x2": "ld(4)", "y2": "ld(5)+2*ld(3)",
        "x3": "ld(4)+2*ld(2)", "y3": "ld(5)+2*ld(3)",
    }
    # Each corner expression recomputes the registers; the sum's value is
    # discarded by multiplying by zero.
    args = ":".join(f"{k}='0*({pre})+{v}'" for k, v in corner.items())
    # Outside every zoom the envelope is zero and the frame passes untouched: the filter
    # is switched off there instead of remapping identical pixels.
    windows = "+".join(f"between(t,{z['start'] - 1 / fps:.4f},{z['end'] + 1 / fps:.4f})"
                       for z in zooms)
    return (f"perspective={args}:sense=source:interpolation={interpolation}:eval=frame"
            f":enable='gt({windows},0)'")


def track_expr(track: list[list[float]], fps: float) -> str:
    """Piecewise-linear subject centre (0..1) over item-local time, on frame number."""
    T = f"(n/{fps:.6f})"
    expr = f"{track[-1][1]:.4f}"
    for (t0, c0), (t1, c1) in reversed(list(zip(track, track[1:]))):
        if t1 <= t0:
            continue
        seg = f"{c0:.4f}+({c1 - c0:.4f})*({T}-{t0:.3f})/{t1 - t0:.3f}"
        expr = f"if(lt({T},{t1:.3f}),{seg},{expr})"
    return expr


def render_item(src: Path, item: Item, out: Path, ass: Path | None, size: tuple[int, int],
                quality: str, fps: float, zooms: list[dict] | None = None,
                brolls: list[dict] | None = None, lut: Path | None = None,
                reframe: list[list[float]] | None = None, grade: dict | None = None) -> None:
    w, h = size
    rate = fps_fraction(fps)
    if reframe:
        # 9:16 from landscape: a full-height window that follows the subject, clamped
        # inside the frame. Portrait sources keep their full width.
        cx = track_expr(reframe, fps)
        fit = [f"crop=w='min(iw,ih*{w}/{h})':h=ih:x='clip(({cx})*iw-ow/2,0,iw-ow)':y=0",
               f"scale={w}:{h}:flags=lanczos"]
    else:
        fit = [f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"]
    base = [
        f"fps={rate}",
        *fit,
        "setsar=1",
        # A source that ends a frame early must not shorten the item, or every
        # later cut drifts against the audio. Clone the last frame instead.
        "tpad=stop_mode=clone:stop_duration=1",
    ]
    if zooms:
        base.append(zoom_filter(zooms, fps, "cubic" if quality == "master" else "linear"))
    graph = [f"[0:v]{','.join(base)}[v0]"]
    inputs = ["-ss", f"{item.src_in:.6f}", "-i", str(src)]
    last = "v0"
    for n, br in enumerate(brolls or [], 1):
        inputs += ["-ss", f"{br['src_in']:.6f}", "-t", f"{br['end'] - br['start'] + 0.5:.3f}",
                   "-i", str(br["src"])]
        graph.append(f"[{n}:v]fps={rate},scale={w}:{h}:force_original_aspect_ratio=increase"
                     f":flags=lanczos,crop={w}:{h},setsar=1,"
                     f"setpts=PTS-STARTPTS+{br['start']:.4f}/TB[b{n}]")
        graph.append(f"[{last}][b{n}]overlay=enable='between(t,{br['start']:.4f},"
                     f"{br['end'] - 0.5 / fps:.4f})':eof_action=pass[v{n}]")
        last = f"v{n}"
    tail = []
    if lut is not None:
        tail.append(f"lut3d=file='{_filter_path(lut)}':interp=tetrahedral")
    elif grade:
        from . import fx
        tail.append(fx.preview_grade_filter(grade))
    if ass is not None:
        tail.append(f"ass=filename='{_filter_path(ass)}'")
    tail.append("format=yuv420p")
    graph.append(f"[{last}]{','.join(tail)}[out]")
    script = out.with_suffix(".filter")
    script.write_text(";\n".join(graph))
    tmp = out.with_name(f".{out.name}")
    media.ffmpeg(*inputs, "-filter_complex_script", str(script), "-map", "[out]", "-an",
                 "-frames:v", str(item.frames), "-r", str(rate),
                 *media.encode_args(quality, fps),
                 "-video_track_timescale", str(track_timescale(fps)),
                 "-f", "mp4", str(tmp))
    os.replace(tmp, out)
    script.unlink(missing_ok=True)


def assemble_audio(project: Project, items: list[Item], clips: dict, fps: float) -> np.ndarray:
    """Cut the mix from each clip's PCM at sample-exact frame boundaries.

    Item edges come from cumulative frame counts, so the audio can never drift
    from the stream-copied video, however many cuts there are.
    """
    sr, ch = config.MIX_SAMPLE_RATE, config.MIX_CHANNELS
    total_frames = sum(it.frames for it in items)
    total_samples = round(total_frames * sr / fps)
    mix = np.zeros((total_samples, ch), dtype=np.float32)
    sources: dict[str, np.ndarray | None] = {}
    edge = max(1, round(config.EDGE_FADE_S * sr))

    cursor = 0
    for k, it in enumerate(items):
        o0 = round(cursor * sr / fps)
        o1 = round((cursor + it.frames) * sr / fps)
        cursor += it.frames
        if it.clip_id not in sources:
            rel = clips[it.clip_id].get("mix_audio")
            path = project.path(rel) if rel else None
            sources[it.clip_id] = (np.memmap(path, dtype=np.int16, mode="r").reshape(-1, ch)
                                   if path and path.exists() and path.stat().st_size else None)
        src = sources[it.clip_id]
        if src is None:
            continue
        s0 = round(it.src_in * sr)
        chunk = src[s0: s0 + (o1 - o0)].astype(np.float32) / 32768.0
        seg = np.zeros((o1 - o0, ch), dtype=np.float32)
        seg[: len(chunk)] = chunk
        # Short fades only at real cuts; a continuous take gets none, or the
        # join would dip audibly.
        n = min(edge, len(seg) // 2)
        if n and (k == 0 or not contiguous(items[k - 1], it, fps)):
            seg[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
        if n and (k == len(items) - 1 or not contiguous(it, items[k + 1], fps)):
            seg[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)[:, None]
        mix[o0:o1] = seg

    return mix


def write_wav(mix: np.ndarray, out: Path) -> None:
    pcm = np.clip(mix * 32767.0, -32768, 32767).astype("<i2")
    tmp = out.with_name(f".{out.name}")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(mix.shape[1])
        wf.setsampwidth(2)
        wf.setframerate(config.MIX_SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    os.replace(tmp, out)


def _db(x: np.ndarray) -> float:
    return float(20 * np.log10(max(np.sqrt(np.mean(x * x)) if x.size else 0.0, 1e-6)))


def speech_level_db(mix: np.ndarray, speech: list[tuple[float, float]]) -> float:
    sr = config.MIX_SAMPLE_RATE
    parts = [mix[int(a * sr):int(b * sr)] for a, b in speech if b > a]
    return _db(np.concatenate(parts)) if parts else -30.0


def cut_report(dialogue: np.ndarray, cuts: list[float]) -> dict:
    """Gate 1, no cut mid-word: dialogue level in the 30 ms either side of every cut,
    relative to the loud end of speech. Measured before music hides anything."""
    sr = config.MIX_SAMPLE_RATE
    win = int(0.03 * sr)
    frames = [dialogue[i:i + win] for i in range(0, len(dialogue) - win, win)]
    speech = float(np.percentile([_db(f) for f in frames], 90)) if frames else -30.0
    worst = None
    for t in cuts:
        i = int(t * sr)
        level = max(_db(dialogue[max(0, i - win):i]), _db(dialogue[i:i + win])) - speech
        worst = level if worst is None else max(worst, level)
    return {"cuts_checked": len(cuts), "worst_cut_db": round(worst, 1) if worst is not None
            else None, "cuts_clean": worst is None or worst < -20.0}


def mix_audio(dialogue: np.ndarray, meta: dict, words: list[dict]) -> np.ndarray:
    """Dialogue plus the ducked music bed and SFX, before loudness normalisation."""
    from . import fx, music

    sr = config.MIX_SAMPLE_RATE
    mix = dialogue.copy()
    speech = [(w["s"], w["e"]) for w in words]
    ref = speech_level_db(dialogue, speech)
    m = meta.get("music")
    if m:
        bed = music.render_bed(m, [tuple(p) for p in m["pieces"]], len(mix), m["crossfade_s"])
        # Level the bed to the dialogue, then the envelope applies the style's depths.
        bed *= 10 ** ((ref - _db(bed[bed.any(axis=1)] if bed.any() else bed)) / 20)
        beats = [(b["start_s"], b["end_s"], b.get("music_energy", "mid"))
                 for b in meta.get("beats", [])]
        bed *= music.gain_envelope(len(mix), speech, beats, m["under_speech_db"], m["gap_db"])
        mix += bed
    for cue in meta.get("sfx", []):
        sample = fx.sfx_sample(cue["kind"])
        gain = 10 ** ((ref - 10 - _db(sample)) / 20)  # 10 dB under speech: felt, not heard
        i = int(cue["t"] * sr)
        n = min(len(sample), len(mix) - i)
        if n > 0:
            mix[i:i + n] += (sample[:n] * gain)[:, None]
    return mix


def _loudnorm_json(stderr: str) -> dict:
    blocks = re.findall(r"\{[^{}]*\}", stderr)
    return json.loads(blocks[-1]) if blocks else {}


def normalise_audio(wav: Path, out: Path) -> None:
    """Two-pass EBU R128 to the delivery target: −14 LUFS, −1 dBTP."""
    target = (f"I={config.LOUDNESS_TARGET_LUFS}:TP={config.TRUE_PEAK_DBTP}"
              f":LRA={config.LOUDNESS_RANGE}")
    first = media.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(wav), "-af",
                       f"loudnorm={target}:print_format=json", "-f", "null", "-"])
    m = _loudnorm_json(first.stderr)
    tmp = out.with_name(f".{out.name}")
    encode = ["-c:a", "aac", "-b:a", config.AUDIO_BITRATE, "-ar", str(config.MIX_SAMPLE_RATE)]
    if not m or m.get("input_i") in ("-inf", "inf"):
        # Silence cannot be normalised; ship it as is rather than fail.
        media.ffmpeg("-i", str(wav), *encode, "-f", "mp4", str(tmp))
    else:
        measured = (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                    f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                    f":offset={m['target_offset']}:linear=true")
        media.ffmpeg("-i", str(wav), "-af", f"loudnorm={target}{measured}", *encode,
                     "-f", "mp4", str(tmp))
    os.replace(tmp, out)


def measure_loudness(path: Path) -> dict:
    """Independent EBU R128 measurement of the delivered file, not loudnorm's estimate."""
    proc = media.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0",
                      "-af", "ebur128=peak=true", "-f", "null", "-"])
    summary = proc.stderr.rsplit("Summary:", 1)[-1]
    i = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", summary)
    tp = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", summary)
    as_float = lambda m: float(m.group(1)) if m and m.group(1) != "-inf" else None  # noqa: E731
    return {"integrated_lufs": as_float(i), "true_peak_dbtp": as_float(tp)}


def render(project: Project, timeline_path: Path, quality: str = "master",
           size: tuple[int, int] | None = None, jobs: int | None = None,
           log: Log = print, progress: Callable[[float, str], None] | None = None,
           fmt: str = "16:9") -> dict:
    progress = progress or (lambda f, m: None)
    media.require_ffmpeg()
    meta, items = timeline.load(timeline_path)
    if not items:
        raise SystemExit("timeline is empty: nothing to render")
    fps = meta["fps"]
    clips = meta["clips"]
    size = size or (config.MASTER_SIZE if quality == "master" else config.PREVIEW_SIZE)
    vertical = fmt == "9:16"
    if vertical:
        size = {"master": (1080, 1920), "preview": (720, 1280)}[quality]
    reframes = meta.get("reframe") or []
    cap = meta.get("captions") or {}
    style = cap.get("style", "clean")
    words = captions.output_words(items, fps, meta.get("emphasis"))
    events = captions.build_events(words, style, cap) if cap.get("enabled") else []
    lut, grade = None, None
    if meta.get("colour"):
        from . import fx
        if not fx.is_identity(meta["colour"]):
            if quality == "master":
                lut = fx.write_cube(project.path("renders", "cache",
                                                 f"lut_{stable_hash(meta['colour'])}.cube"),
                                    meta["colour"])
            else:
                grade = {k: meta["colour"][k] for k in ("saturation", "contrast", "warmth",
                                                         "lift") if k in meta["colour"]}

    cache = project.path("renders", "cache")
    jobs_todo: list[tuple] = []
    seg_paths: list[Path] = []
    cursor = 0
    enc = media.encode_args(quality, fps)
    for k, it in enumerate(items):
        t0, t1 = cursor / fps, (cursor + it.frames) / fps
        cursor += it.frames
        track = (reframes[k] if k < len(reframes) else [[0.0, 0.5]]) if vertical else None
        ass_text = None
        if events:
            ass_text = captions.ass_document(captions.slice_events(events, t0, t1),
                                             size[0], size[1], style, cap.get("scale", 1.0))
        clip = clips[it.clip_id]
        src = Path(clip["path"]) if quality == "master" else project.path(clip["proxy"])
        # Only what changes pixels: a zoom's rule text or anchor must not void the cache.
        zooms = [{"start": round(z["start_s"] - t0, 4), "end": round(z["end_s"] - t0, 4),
                  "scale": z["scale"], "cx": z["cx"], "cy": z["cy"], "ease_s": z["ease_s"],
                  "kind": z["kind"]}
                 for z in meta.get("zooms", []) if z["start_s"] < t1 and z["end_s"] > t0]
        brolls = []
        for br in meta.get("broll", []):
            a, b = max(br["start_s"], t0), min(br["end_s"], t1)
            if b - a >= 1 / fps:
                bc = clips[br["clip_id"]]
                brolls.append({"start": a - t0, "end": b - t0,
                               "src_in": br["src_in"] + (a - br["start_s"]),
                               "src": str(Path(bc["path"]) if quality == "master"
                                          else project.path(bc["proxy"]))})
        key = stable_hash({"v": RENDER_VERSION, "clip": it.clip_id, "src": quality,
                           "in": it.src_in, "frames": it.frames, "fps": fps, "size": size,
                           "enc": enc, "ass": ass_text, "zooms": zooms,
                           "broll": [{k: v for k, v in b.items() if k != "src"} for b in brolls]
                           + [b["src"] for b in brolls], "lut": str(lut) if lut else None,
                           "grade": grade, "reframe": track})
        out = cache / f"seg_{key}.mp4"
        seg_paths.append(out)
        if not out.exists():
            ass_path = None
            if ass_text is not None:
                ass_path = cache / f"seg_{key}.ass"
                ass_path.write_text(ass_text)
            jobs_todo.append((src, it, out, ass_path, zooms, brolls, track))

    missing = {j[0] for j in jobs_todo if not j[0].exists()} | {
        Path(b["src"]) for j in jobs_todo for b in j[5] if not Path(b["src"]).exists()}
    if missing:
        raise SystemExit("missing source media:\n  " + "\n  ".join(map(str, sorted(missing))))

    log(f"render {quality} {size[0]}x{size[1]} @ {fps:g} fps with {media.video_encoder()}: "
        f"{len(jobs_todo)} of {len(items)} items to encode, {len(items) - len(jobs_todo)} cached")
    workers = jobs or int(os.environ.get("NULLCUT_RENDER_JOBS", min(4, os.cpu_count() or 1)))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(render_item, src, it, out, ass, size, quality, fps, zooms,
                               brolls, lut, track, grade)
                   for src, it, out, ass, zooms, brolls, track in jobs_todo]
        for n, f in enumerate(futures, 1):
            f.result()
            progress(0.85 * n / len(futures), f"encoded {n}/{len(futures)} items")
            if n % 25 == 0 or n == len(futures):
                log(f"  encoded {n}/{len(futures)}")

    version = meta["version"]
    kind = meta.get("stem", "rough")
    stem = f"{kind}.v{version}.{quality}" + (".vertical" if vertical else "")
    renders = project.path("renders")

    concat_list = cache / f"{stem}.concat.txt"
    concat_list.write_text("".join(_concat_line(p) for p in seg_paths))
    video = cache / f"{stem}.video.mp4"
    media.ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", "-an",
                 str(video))

    audio_key = stable_hash({"v": RENDER_VERSION, "fps": fps,
                             "items": [(it.clip_id, it.src_in, it.frames) for it in items],
                             "music": meta.get("music"), "sfx": meta.get("sfx"),
                             "beats": meta.get("beats")})
    mix_wav = cache / f"mix_{audio_key}.wav"
    mix_m4a = cache / f"mix_{audio_key}.m4a"
    qc_path = cache / f"mix_{audio_key}.cuts.json"
    progress(0.88, "mixing audio")
    audio_cached = mix_m4a.exists() and qc_path.exists()
    if not audio_cached:
        log("mix audio and normalise to "
            f"{config.LOUDNESS_TARGET_LUFS:g} LUFS / {config.TRUE_PEAK_DBTP:g} dBTP")
        dialogue = assemble_audio(project, items, clips, fps)
        starts = np.cumsum([0] + [it.frames for it in items])[:-1] / fps
        cuts = [float(starts[k + 1]) for k in range(len(items) - 1)
                if not contiguous(items[k], items[k + 1], fps)]
        write_json(qc_path, cut_report(dialogue, cuts))
        write_wav(mix_audio(dialogue, meta, words), mix_wav)
        normalise_audio(mix_wav, mix_m4a)
        mix_wav.unlink(missing_ok=True)

    final = renders / f"{stem}.mp4"
    tmp = final.with_name(f".{final.name}")
    media.ffmpeg("-i", str(video), "-i", str(mix_m4a), "-map", "0:v:0", "-map", "1:a:0",
                 "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp))
    os.replace(tmp, final)
    video.unlink(missing_ok=True)

    srt_path = renders / f"{kind}.v{version}.srt"
    vtt_path = renders / f"{kind}.v{version}.vtt"
    srt_path.write_text(captions.srt(words))
    vtt_path.write_text(captions.vtt(words))

    loud = measure_loudness(final)
    return {
        "output": str(final), "srt": str(srt_path), "vtt": str(vtt_path),
        "encoded": len(jobs_todo), "cached": len(items) - len(jobs_todo),
        "audio_cached": audio_cached,
        "expected_s": round(sum(it.frames for it in items) / fps, 3),
        "video_s": round(media.stream_duration(final, "v"), 3),
        "audio_s": round(media.stream_duration(final, "a"), 3),
        **loud,
        **read_json(qc_path),
    }
