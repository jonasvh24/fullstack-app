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
from .project import Project, stable_hash

# Bump when render output changes for identical inputs; old cache entries
# simply stop matching.
RENDER_VERSION = 1

Log = Callable[[str], None]


def fps_fraction(fps: float) -> Fraction:
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


def render_item(src: Path, item: Item, out: Path, ass: Path | None, size: tuple[int, int],
                quality: str, fps: float) -> None:
    w, h = size
    rate = fps_fraction(fps)
    chain = [
        f"fps={rate}",
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        # A source that ends a frame early must not shorten the item, or every
        # later cut drifts against the audio. Clone the last frame instead.
        "tpad=stop_mode=clone:stop_duration=1",
    ]
    if ass is not None:
        chain.append(f"ass=filename='{_filter_path(ass)}'")
    chain.append("format=yuv420p")
    tmp = out.with_name(f".{out.name}")
    media.ffmpeg("-ss", f"{item.src_in:.6f}", "-i", str(src), "-map", "0:v:0", "-an",
                 "-vf", ",".join(chain), "-frames:v", str(item.frames), "-r", str(rate),
                 *media.encode_args(quality, fps),
                 "-video_track_timescale", str(track_timescale(fps)),
                 "-f", "mp4", str(tmp))
    os.replace(tmp, out)


def assemble_audio(project: Project, items: list[Item], clips: dict, fps: float,
                   out: Path) -> None:
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

    pcm = np.clip(mix * 32767.0, -32768, 32767).astype("<i2")
    tmp = out.with_name(f".{out.name}")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    os.replace(tmp, out)


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
           log: Log = print) -> dict:
    media.require_ffmpeg()
    meta, items = timeline.load(timeline_path)
    if not items:
        raise SystemExit("timeline is empty: nothing to render")
    fps = meta["fps"]
    clips = meta["clips"]
    size = size or (config.MASTER_SIZE if quality == "master" else config.PREVIEW_SIZE)
    cap = meta.get("captions") or {}
    style = cap.get("style", "clean")
    words = captions.output_words(items, fps)
    events = captions.build_events(words, style) if cap.get("enabled") else []

    cache = project.path("renders", "cache")
    jobs_todo: list[tuple] = []
    seg_paths: list[Path] = []
    cursor = 0
    enc = media.encode_args(quality, fps)
    for it in items:
        t0, t1 = cursor / fps, (cursor + it.frames) / fps
        cursor += it.frames
        ass_text = None
        if events:
            ass_text = captions.ass_document(captions.slice_events(events, t0, t1),
                                             size[0], size[1], style)
        clip = clips[it.clip_id]
        src = Path(clip["path"]) if quality == "master" else project.path(clip["proxy"])
        key = stable_hash({"v": RENDER_VERSION, "clip": it.clip_id, "src": quality,
                           "in": it.src_in, "frames": it.frames, "fps": fps, "size": size,
                           "enc": enc, "ass": ass_text})
        out = cache / f"seg_{key}.mp4"
        seg_paths.append(out)
        if not out.exists():
            ass_path = None
            if ass_text is not None:
                ass_path = cache / f"seg_{key}.ass"
                ass_path.write_text(ass_text)
            jobs_todo.append((src, it, out, ass_path))

    missing = {j[0] for j in jobs_todo if not j[0].exists()}
    if missing:
        raise SystemExit("missing source media:\n  " + "\n  ".join(map(str, sorted(missing))))

    log(f"render {quality} {size[0]}x{size[1]} @ {fps:g} fps with {media.video_encoder()}: "
        f"{len(jobs_todo)} of {len(items)} items to encode, {len(items) - len(jobs_todo)} cached")
    workers = jobs or int(os.environ.get("NULLCUT_RENDER_JOBS", min(4, os.cpu_count() or 1)))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(render_item, src, it, out, ass, size, quality, fps)
                   for src, it, out, ass in jobs_todo]
        for n, f in enumerate(futures, 1):
            f.result()
            if n % 25 == 0 or n == len(futures):
                log(f"  encoded {n}/{len(futures)}")

    version = meta["version"]
    stem = f"rough.v{version}.{quality}"
    renders = project.path("renders")

    concat_list = cache / f"{stem}.concat.txt"
    concat_list.write_text("".join(_concat_line(p) for p in seg_paths))
    video = cache / f"{stem}.video.mp4"
    media.ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", "-an",
                 str(video))

    audio_key = stable_hash({"v": RENDER_VERSION, "fps": fps,
                             "items": [(it.clip_id, it.src_in, it.frames) for it in items]})
    mix_wav = cache / f"mix_{audio_key}.wav"
    mix_m4a = cache / f"mix_{audio_key}.m4a"
    if not mix_m4a.exists():
        log("mix audio and normalise to "
            f"{config.LOUDNESS_TARGET_LUFS:g} LUFS / {config.TRUE_PEAK_DBTP:g} dBTP")
        assemble_audio(project, items, clips, fps, mix_wav)
        normalise_audio(mix_wav, mix_m4a)
        mix_wav.unlink(missing_ok=True)

    final = renders / f"{stem}.mp4"
    tmp = final.with_name(f".{final.name}")
    media.ffmpeg("-i", str(video), "-i", str(mix_m4a), "-map", "0:v:0", "-map", "1:a:0",
                 "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp))
    os.replace(tmp, final)
    video.unlink(missing_ok=True)

    srt_path = renders / f"rough.v{version}.srt"
    vtt_path = renders / f"rough.v{version}.vtt"
    srt_path.write_text(captions.srt(words))
    vtt_path.write_text(captions.vtt(words))

    loud = measure_loudness(final)
    return {
        "output": str(final), "srt": str(srt_path), "vtt": str(vtt_path),
        "encoded": len(jobs_todo), "cached": len(items) - len(jobs_todo),
        "expected_s": round(sum(it.frames for it in items) / fps, 3),
        "video_s": round(media.stream_duration(final, "v"), 3),
        "audio_s": round(media.stream_duration(final, "a"), 3),
        **loud,
    }
