"""Visual half of the Clip Index (plan §4): shots, keyframes, faces, motion and quality.

One decode of the 720p proxy at 5 fps feeds every per-frame signal. Shot
boundaries come from FFmpeg's scene score at full frame rate instead, because
a B-roll cut one frame late shows a flash frame of the next shot.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from . import llm, media, models

SAMPLE_FPS = 5.0
DECODE_W = 480          # keyframes double as storyboard thumbnails and VLM input
FLOW_W = 160            # optical flow needs structure, not detail
KEYFRAME_EVERY_S = 2.0  # plan §4: one per shot plus every 2 s
SCENE_THRESHOLD = 0.3
MIN_SHOT_S = 0.5
FACE_SCORE = 0.7
VLM_PROMPT = ("Describe this video frame in one sentence for a video editor: who or what is "
              "in it, the action, the setting and the shot type (wide, medium, close-up).")

Log = Callable[[str], None]


def detect_shots(proxy: Path, duration: float) -> list[tuple[float, float]]:
    out = media.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(proxy), "-an",
                     "-vf", f"scale={FLOW_W}:-2,select='gt(scene,{SCENE_THRESHOLD})',"
                            "metadata=print:file=-",
                     "-f", "null", "-"]).stdout
    cuts = [float(m) for m in re.findall(r"pts_time:([\d.]+)", out)]
    bounds = [0.0]
    for c in cuts:
        if c - bounds[-1] >= MIN_SHOT_S and duration - c >= MIN_SHOT_S:
            bounds.append(c)
    bounds.append(duration)
    return list(zip(bounds, bounds[1:]))


def sample_frames(proxy: Path, width: int, height: int) -> Iterator[tuple[float, np.ndarray]]:
    """Stream BGR frames at SAMPLE_FPS without touching disk."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(proxy),
           "-an", "-vf", f"fps={SAMPLE_FPS},scale={width}:{height}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    frame_bytes = width * height * 3
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        n = 0
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield n / SAMPLE_FPS, np.frombuffer(buf, np.uint8).reshape(height, width, 3)
            n += 1


class FaceDetector:
    def __init__(self, width: int, height: int):
        import cv2

        # Errors only: the DNN backend warns on every model load. The API moved in OpenCV 5.
        logging = getattr(getattr(cv2, "utils", None), "logging", None)
        if logging is not None:
            logging.setLogLevel(logging.LOG_LEVEL_ERROR)
        elif hasattr(cv2, "setLogLevel"):
            cv2.setLogLevel(2)
        self.model = None
        name = models.DEFAULT_PROFILE["faces"]
        if models.installed(name):
            onnx = models.path_of(name) / models.REGISTRY[name].files[0]
            self.model = cv2.FaceDetectorYN.create(str(onnx), "", (width, height), FACE_SCORE)
        self.w, self.h = width, height

    @property
    def available(self) -> bool:
        return self.model is not None

    def largest(self, frame: np.ndarray) -> list[float] | None:
        """Largest face as normalised [x, y, w, h, score]: the subject for zooms and reframes."""
        if self.model is None:
            return None
        _, faces = self.model.detect(frame)
        if faces is None or len(faces) == 0:
            return None
        f = max(faces, key=lambda r: r[2] * r[3])
        return [round(float(f[0]) / self.w, 4), round(float(f[1]) / self.h, 4),
                round(float(f[2]) / self.w, 4), round(float(f[3]) / self.h, 4),
                round(float(f[-1]), 3)]


def analyse_visual(project, clip: dict, log: Log = print) -> dict:
    import cv2

    proxy = project.path(clip["proxy"])
    duration = clip["duration"]
    src_w, src_h = clip["width"] or 16, clip["height"] or 9
    width = DECODE_W
    height = max(2, int(round(width * src_h / src_w / 2)) * 2)
    flow_h = max(2, int(round(FLOW_W * src_h / src_w / 2)) * 2)

    shots = detect_shots(proxy, duration)
    key_times = sorted({round(t * SAMPLE_FPS) / SAMPLE_FPS for t in
                        [(a + b) / 2 for a, b in shots]
                        + list(np.arange(0.0, duration, KEYFRAME_EVERY_S))})
    key_set = {round(t * 1000) for t in key_times}
    kf_dir = project.path("index", "keyframes", clip["id"])
    kf_dir.mkdir(parents=True, exist_ok=True)

    faces = FaceDetector(width, height)
    samples: list[dict] = []
    keyframes: dict[int, str] = {}
    prev_small = None
    for t, frame in sample_frames(proxy, width, height):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (FLOW_W, flow_h), interpolation=cv2.INTER_AREA)
        motion = 0.0
        moving = None
        if prev_small is not None:
            # Where the picture changes: the subject of a screen recording (cursor, typing).
            delta = cv2.absdiff(prev_small, small) > 20
            if delta.mean() > 0.002:
                ys, xs = np.nonzero(delta)
                moving = [round(float(np.median(xs)) / FLOW_W, 4),
                          round(float(np.median(ys)) / flow_h, 4), round(float(delta.mean()), 4)]
            flow = cv2.calcOpticalFlowFarneback(prev_small, small, None, 0.5, 2, 9, 2, 5, 1.1, 0)
            # Pixels per second at flow width, so the number means the same at any frame rate.
            motion = float(np.median(np.linalg.norm(flow, axis=2))) * SAMPLE_FPS
        prev_small = small
        samples.append({
            "t": round(t, 3),
            "motion": round(motion, 2),
            "sharp": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1),
            "luma": round(float(gray.mean()) / 255, 3),
            "clip": round(float(((gray < 8) | (gray > 247)).mean()), 3),
            "face": faces.largest(frame),
            "moving": moving,
        })
        ms = round(t * 1000)
        if ms in key_set:
            rel = f"index/keyframes/{clip['id']}/{ms:08d}.jpg"
            cv2.imwrite(str(project.path(rel)), frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            keyframes[ms] = rel

    per_second = _per_second(samples, duration)
    captions = _caption_keyframes(project, keyframes, shots, log, clip["name"])
    shot_records = []
    for n, (a, b) in enumerate(shots):
        mid = nearest_keyframe(keyframes, (a + b) / 2)
        window = [s for s in samples if a <= s["t"] < b] or samples[:1]
        shot_records.append({
            "id": f"{clip['id']}:shot{n:03d}", "start": round(a, 3), "end": round(b, 3),
            "keyframe": mid[1] if mid else None,
            "caption": captions.get(mid[0], "") if mid else "",
            "motion": round(float(np.median([s["motion"] for s in window])), 2) if window else 0.0,
            "sharp": round(float(np.median([s["sharp"] for s in window])), 1) if window else 0.0,
            "luma": round(float(np.mean([s["luma"] for s in window])), 3) if window else 0.0,
            "face": round(float(np.mean([s["face"] is not None for s in window])), 2)
            if window else 0.0,
        })
    return {
        "faces_available": faces.available,
        "shots": shot_records,
        "keyframes": {str(k): v for k, v in sorted(keyframes.items())},
        "captions": {str(k): v for k, v in sorted(captions.items())},
        "faces": [[s["t"], *s["face"]] for s in samples if s["face"]],
        # Only where no face is seen: a face always outranks motion as the subject.
        "motion": [[s["t"], *s["moving"]] for s in samples if s["moving"] and not s["face"]],
        "quality": per_second,
    }


def _per_second(samples: list[dict], duration: float) -> dict:
    n = max(1, int(np.ceil(duration)))
    out = {"motion": [0.0] * n, "sharp": [0.0] * n, "luma": [0.0] * n, "clipped": [0.0] * n}
    buckets: dict[int, list[dict]] = {}
    for s in samples:
        buckets.setdefault(min(n - 1, int(s["t"])), []).append(s)
    for i, group in buckets.items():
        out["motion"][i] = round(float(np.mean([g["motion"] for g in group])), 2)
        out["sharp"][i] = round(float(np.mean([g["sharp"] for g in group])), 1)
        out["luma"][i] = round(float(np.mean([g["luma"] for g in group])), 3)
        out["clipped"][i] = round(float(np.mean([g["clip"] for g in group])), 3)
    return out


def nearest_keyframe(keyframes: dict, t: float) -> tuple[int, str] | None:
    """Accepts int keys (in memory) or str keys (read back from JSON)."""
    if not keyframes:
        return None
    by_ms = {int(k): v for k, v in keyframes.items()}
    ms = min(by_ms, key=lambda k: abs(k - t * 1000))
    return ms, by_ms[ms]


def _caption_keyframes(project, keyframes: dict[int, str], shots, log: Log,
                       name: str) -> dict[int, str]:
    ep = llm.vlm_endpoint()
    if ep is None or not keyframes:
        return {}
    captions: dict[int, str] = {}
    for n, (ms, rel) in enumerate(sorted(keyframes.items()), 1):
        msg = [{"role": "user", "content": [{"type": "text", "text": VLM_PROMPT},
                                            llm.image_part(project.path(rel))]}]
        try:
            reply = llm.chat(ep, msg, max_tokens=80, temperature=0.0)
            captions[ms] = (reply.get("content") or "").strip()
        except llm.LLMError as exc:
            log(f"{name}: scene captions stopped at keyframe {n}: {exc}")
            break
        if n % 50 == 0:
            log(f"{name}: captioned {n}/{len(keyframes)} keyframes")
    return captions
