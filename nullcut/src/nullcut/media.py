"""FFmpeg plumbing: probing, encoder choice, proxies and audio extraction.

FFmpeg is always invoked as a separate binary so its GPL components stay at
arm's length from this codebase (plan §11).
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import config


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc


def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args])


@dataclass
class Probe:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    has_video: bool


def _parse_rate(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    return float(Fraction(rate))


def probe(path: Path) -> Probe:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]).stdout
    info = json.loads(out)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = any(s["codec_type"] == "audio" for s in info["streams"])
    duration = float(info["format"].get("duration") or 0.0)
    if video is None:
        return Probe(duration, 0, 0, 0.0, audio, False)

    w, h = int(video["width"]), int(video["height"])
    # Phone footage stores portrait as landscape plus a rotation flag. FFmpeg
    # autorotates on decode, so the probe must report post-rotation geometry.
    rotation = 0
    for sd in video.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = int(video.get("tags", {}).get("rotate", rotation))
    if abs(rotation) % 180 == 90:
        w, h = h, w

    # avg_frame_rate is honest for VFR phone clips; r_frame_rate can report
    # the container tick (e.g. 90000/1) instead of a frame rate.
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    if fps > 240 or fps <= 0:
        fps = _parse_rate(video.get("r_frame_rate")) or config.DEFAULT_FPS
    return Probe(duration, w, h, round(fps, 3), audio, True)


@functools.lru_cache(maxsize=1)
def video_encoder() -> str:
    """NVENC when it actually encodes, libx264 otherwise.

    Distro FFmpeg builds list h264_nvenc even on machines with no NVIDIA
    driver, so listing the encoder proves nothing; a real encode does.
    """
    forced = os.environ.get("NULLCUT_ENCODER")
    if forced:
        return forced
    try:
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=256x256:d=0.2", "-c:v", "h264_nvenc",
               "-f", "null", "-")
        return "h264_nvenc"
    except FFmpegError:
        return "libx264"


def encode_args(quality: str, fps: float) -> list[str]:
    """Identical settings per quality so per-beat files concat by stream copy."""
    enc = video_encoder()
    gop = str(max(1, round(fps * 2)))
    common = ["-pix_fmt", "yuv420p", "-g", gop]
    if enc == "h264_nvenc":
        preset, cq = {"master": ("p5", "19"), "preview": ("p1", "28"),
                      "proxy": ("p2", "26")}[quality]
        return ["-c:v", enc, "-preset", preset, "-rc", "vbr", "-cq", cq, "-b:v", "0",
                "-profile:v", "high", *common]
    preset, crf = {"master": ("medium", "18"), "preview": ("ultrafast", "28"),
                   "proxy": ("veryfast", "26")}[quality]
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-profile:v", "high", *common]


def make_proxy(src: Path, dst: Path, probe_: Probe) -> None:
    if dst.exists():
        return
    tmp = dst.with_name(f".{dst.name}")
    # -2 keeps width even, which H.264 requires.
    height = min(config.PROXY_HEIGHT, probe_.height or config.PROXY_HEIGHT)
    audio = ["-c:a", "aac", "-b:a", "128k"] if probe_.has_audio else ["-an"]
    ffmpeg("-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", f"scale=-2:{height}", *encode_args("proxy", probe_.fps or config.DEFAULT_FPS),
           *audio, "-movflags", "+faststart", "-f", "mp4", str(tmp))
    os.replace(tmp, dst)


def extract_audio(src: Path, asr_wav: Path, mix_pcm: Path) -> None:
    """One decode, two outputs: 16 kHz mono for ASR, 48 kHz stereo PCM for the mix."""
    if asr_wav.exists() and mix_pcm.exists():
        return
    tmp_wav = asr_wav.with_name(f".{asr_wav.name}")
    tmp_pcm = mix_pcm.with_name(f".{mix_pcm.name}")
    ffmpeg("-i", str(src), "-vn",
           "-map", "0:a:0", "-ac", "1", "-ar", str(config.ASR_SAMPLE_RATE),
           "-c:a", "pcm_s16le", "-f", "wav", str(tmp_wav),
           "-map", "0:a:0", "-ac", str(config.MIX_CHANNELS), "-ar", str(config.MIX_SAMPLE_RATE),
           "-c:a", "pcm_s16le", "-f", "s16le", str(tmp_pcm))
    os.replace(tmp_wav, asr_wav)
    os.replace(tmp_pcm, mix_pcm)


def media_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    return float(out)


def stream_duration(path: Path, kind: str) -> float:
    out = run(["ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
               "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1",
               str(path)]).stdout.strip()
    return float(out)
