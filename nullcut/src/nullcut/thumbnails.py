"""Thumbnail engine (plan §9).

Thumbnails are built from the creator's own best frame; generation never
supplies a face. A generated face is a misleading thumbnail and an off-brand
one. Each candidate is checked at 240 px wide, the size it is actually seen
at: text contrast at least 4.5:1, a readable face, nothing under the
timestamp corner. A failed check means recompose, not ship.

The ranking is a heuristic, not a click-through forecast: confidence that it
picks winners is low until it is A/B tested on real uploads [LOW].
"""

from __future__ import annotations

import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np

from . import config, llm, media, models, vision

W, H = 1280, 720
CHECK_W = 240
CORNER = (0.76, 0.80)            # timestamp badge: right 24 %, bottom 20 %
HERE = Path(__file__).parent / "thumb_profiles"
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "that", "this",
        "we", "i", "you", "for", "with", "be", "are", "was", "here", "there", "what", "how",
        "so", "let", "us", "okay", "well", "um", "uh", "just",
        "de", "het", "een", "en", "van", "dat", "ik", "je", "op", "met", "voor", "dus", "nou"}
# Outro words make a thumbnail read like an ending, wherever the plan put the sentence.
OUTRO = {"thanks", "thank", "watching", "subscribe", "comment", "follow", "bye",
         "bedankt", "kijken", "abonneer", "volg"}

Log = Callable[[str], None]


# --- profiles --------------------------------------------------------------------

def user_dir() -> Path:
    return config.nullcut_home() / "thumb_profiles"


def all_profiles() -> dict[str, dict]:
    out = {}
    for path in sorted(HERE.glob("*.json")):
        p = json.loads(path.read_text())
        out[p["id"]] = {**p, "kind": "shipped"}
    if user_dir().is_dir():
        for path in sorted(user_dir().glob("*.json")):
            try:
                p = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            out[p["id"]] = {**p, "kind": "extracted"}
    return out


def get_profile(pid: str | None) -> dict:
    profiles = all_profiles()
    return profiles.get(pid or "", profiles["reaction-face"])


@lru_cache(maxsize=1)
def _ocr():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def _detector(w: int, h: int) -> vision.FaceDetector:
    return vision.FaceDetector(w, h)


def _faces_full(img: np.ndarray) -> list:
    """All faces with landmarks: [x, y, w, h, 10 landmark coords, score] in pixels."""
    det = _detector(img.shape[1], img.shape[0])
    if det.model is None:
        return []
    _, faces = det.model.detect(img)
    return [] if faces is None else list(faces)


def measure_thumbnail(img: np.ndarray) -> dict:
    """The features a Thumbnail Profile is made of, read from one image."""
    import cv2

    h, w = img.shape[:2]
    faces = _faces_full(img)
    face = max(faces, key=lambda f: f[2] * f[3]) if faces else None
    result, _ = _ocr()(img)
    boxes = [(np.array(b), t) for b, t, s in (result or []) if s > 0.6 and t.strip()]
    words = sum(len(re.findall(r"[\w']+", t)) for _, t in boxes)
    text_h = max((float(b[:, 1].max() - b[:, 1].min()) / h for b, _ in boxes), default=0.0)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    outline = 0.0
    for b, _ in boxes:
        x0, y0 = b.min(0).astype(int)
        x1, y1 = b.max(0).astype(int)
        patch = gray[max(0, y0):y1, max(0, x0):x1]
        if patch.size:
            # Outlined text: both very bright and very dark pixels inside the text box.
            outline = max(outline, float(min((patch > 200).mean(), (patch < 50).mean())))
    sharp_in = sharp_out = None
    if face is not None:
        x, y, fw, fh = map(int, face[:4])
        m = np.zeros(gray.shape, bool)
        m[max(0, y - fh // 2):y + 2 * fh, max(0, x - fw // 2):x + fw + fw // 2] = True
        lap = np.abs(cv2.Laplacian(gray, cv2.CV_64F))
        sharp_in, sharp_out = float(lap[m].mean()), float(lap[~m].mean()) if (~m).any() else 0
    return {
        "face": face is not None,
        "face_share": round(float(face[3]) / h, 3) if face is not None else 0.0,
        "face_x": round(float(face[0] + face[2] / 2) / w, 3) if face is not None else None,
        "words": words, "text_height": round(text_h, 3),
        "saturation": round(float(hsv[..., 1].mean()) / 255, 3),
        "contrast": round(float(gray.std()) / 255, 3),
        "outline": outline > 0.08,
        "cutout": bool(sharp_in and sharp_out is not None and sharp_in > 2.5 * max(sharp_out, 1)),
    }


def extract_profile(name: str, folder: Path, log: Log = print,
                    progress: Callable[[float, str], None] | None = None) -> dict:
    """A Thumbnail Profile from 20-50 reference thumbnails: medians of what they share."""
    import cv2

    progress = progress or (lambda f, m: None)
    files = sorted(f for f in folder.iterdir()
                   if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    if len(files) < 3:
        raise ValueError(f"need reference thumbnails (20 to 50 recommended), found {len(files)}")
    ms = []
    for n, f in enumerate(files):
        progress(n / len(files), f"{f.name}: measuring")
        img = cv2.imread(str(f))
        if img is not None:
            ms.append(measure_thumbnail(cv2.resize(img, (W, H))))
    med = lambda k: float(np.median([m[k] for m in ms if m[k] is not None] or [0]))  # noqa: E731
    faced = np.mean([m["face"] for m in ms])
    base = get_profile("reaction-face" if faced >= 0.5 else "object-hero")
    pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "thumbs"
    profile = {**base, "id": pid, "name": name, "kind": "extracted",
               "description": f"Measured from {len(ms)} reference thumbnails.",
               "face_share": round(max(0.25, med("face_share")), 3) if faced >= 0.5
               else base["face_share"],
               "face_x": round(med("face_x"), 3) if faced >= 0.5 else base["face_x"],
               "words": int(np.clip(round(med("words")), 0, 6)),
               "text_height": round(float(np.clip(med("text_height"), 0.06, 0.3)), 3),
               "saturation": round(float(np.clip(med("saturation") / 0.45, 0.8, 1.6)), 2),
               "contrast": round(float(np.clip(med("contrast") / 0.22, 0.8, 1.5)), 2),
               "outline": bool(np.mean([m["outline"] for m in ms]) >= 0.5),
               "cutout": bool(np.mean([m["cutout"] for m in ms]) >= 0.5),
               "measured": ms}
    user_dir().mkdir(parents=True, exist_ok=True)
    (user_dir() / f"{pid}.json").write_text(json.dumps(profile, indent=2))
    progress(1.0, f"thumbnail profile {name} saved")
    return profile


# --- frames ------------------------------------------------------------------------

def mine_frames(video: Path, words: list[dict], title: str, keep: int = 20) -> list[dict]:
    """Score every half second of the master; keep the best from distinct moments."""
    import cv2

    title_terms = {t for t in re.findall(r"[\w']+", title.lower()) if t not in STOP}
    hits = [w["s"] for w in words if re.sub(r"\W", "", w["w"].lower()) in title_terms]
    cands = []
    prev = None
    for t, frame in _sample(video, 2.0, 640):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        still = 1.0 if prev is None else float(np.exp(-np.abs(gray.astype(float) - prev).mean()
                                                      / 12))
        prev = gray.astype(float)
        faces = _faces_full(frame)
        face = max(faces, key=lambda f: f[2] * f[3]) if faces else None
        expr = 0.0
        if face is not None:
            # YuNet landmarks: mouth corners at indices 10-13. A wider mouth relative to
            # the face reads as a stronger expression; a crude proxy for blendshapes.
            mouth = np.hypot(face[10] - face[12], face[11] - face[13])
            expr = float(np.clip((mouth / max(face[2], 1) - 0.35) / 0.25, 0, 1))
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        colourful = float(hsv[..., 1].mean() / 255)
        near_title = max((np.exp(-abs(t - h) / 3) for h in hits), default=0.0)
        score = (0.30 * (face is not None) + 0.15 * (face[3] / frame.shape[0] if face is not None
                                                      else 0) * 2
                 + 0.15 * expr + 0.15 * min(1.0, sharp / 400) + 0.10 * still
                 + 0.05 * colourful + 0.10 * near_title)
        cands.append({"t": round(t, 2), "score": round(score, 3), "face": face is not None,
                      "expr": round(expr, 2)})
    cands.sort(key=lambda c: -c["score"])
    picked: list[dict] = []
    for c in cands:
        if all(abs(c["t"] - p["t"]) >= 2.0 for p in picked):
            picked.append(c)
        if len(picked) == keep:
            break
    return picked


def _sample(video: Path, fps: float, width: int):
    probe = media.probe(video)
    h = max(2, int(round(width * probe.height / probe.width / 2)) * 2)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(video), "-an",
           "-vf", f"fps={fps},scale={width}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    size = width * h * 3
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        n = 0
        while (buf := proc.stdout.read(size)) and len(buf) == size:
            yield n / fps, np.frombuffer(buf, np.uint8).reshape(h, width, 3)
            n += 1


def source_at(project, timeline_path: Path, t: float) -> tuple[Path, float]:
    """The original clip and source time behind master time t: no captions, no zoom, no grade.

    A thumbnail built from the master would carry the edit's burned-in captions.
    """
    from . import timeline

    meta, items = timeline.load(timeline_path)
    fps = meta["fps"]
    clips = meta["clips"]
    for b in meta.get("broll", []):
        if b["start_s"] <= t < b["end_s"]:
            return Path(clips[b["clip_id"]]["path"]), b["src_in"] + (t - b["start_s"])
    cursor = 0
    for it in items:
        dur = it.frames / fps
        if cursor / fps <= t < cursor / fps + dur:
            return Path(clips[it.clip_id]["path"]), it.src_in + (t - cursor / fps)
        cursor += it.frames
    last = items[-1]
    return Path(clips[last.clip_id]["path"]), last.src_out - 1 / fps


def grab(video: Path, t: float) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                          "-frames:v", "1", "-vf", f"scale={W}:{H}:force_original_aspect_ratio="
                          f"increase,crop={W}:{H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(H, W, 3).copy()


# --- cutout --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _isnet():
    name = models.DEFAULT_PROFILE["cutout"]
    if not models.installed(name):
        return None
    import onnxruntime as ort
    return ort.InferenceSession(str(models.path_of(name) / models.REGISTRY[name].files[0]),
                                providers=["CPUExecutionProvider"])


def cutout_mask(img: np.ndarray) -> tuple[np.ndarray, str]:
    """Subject mask 0..1: IS-Net when pulled, GrabCut seeded by the face otherwise."""
    import cv2

    sess = _isnet()
    if sess is not None:
        x = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), (1024, 1024)).astype(np.float32)
        x = (x / 255.0 - 0.5).transpose(2, 0, 1)[None]
        out = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
        out = (out - out.min()) / max(out.max() - out.min(), 1e-6)
        return cv2.resize(out, (img.shape[1], img.shape[0])), "isnet"
    faces = _faces_full(img)
    h, w = img.shape[:2]
    if faces:
        x, y, fw, fh = map(int, max(faces, key=lambda f: f[2] * f[3])[:4])
        rect = (max(0, x - fw), max(0, y - fh // 2), min(w - 1, 3 * fw), h - max(0, y - fh // 2))
    else:
        rect = (w // 6, h // 8, 2 * w // 3, 3 * h // 4)
    mask = np.zeros((h, w), np.uint8)
    cv2.grabCut(img, mask, rect, np.zeros((1, 65)), np.zeros((1, 65)), 4, cv2.GC_INIT_WITH_RECT)
    m = np.where((mask == 1) | (mask == 3), 1.0, 0.0).astype(np.float32)
    return cv2.GaussianBlur(m, (7, 7), 0), "grabcut"


# --- composition ----------------------------------------------------------------------

def _font(size: int):
    from PIL import ImageFont

    for pattern in ("Inter Display:style=Black", "Inter:style=Black", "DejaVu Sans:style=Bold"):
        try:
            path = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
            if path:
                return ImageFont.truetype(path, size)
        except (OSError, subprocess.SubprocessError):
            continue
    return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _grade(img: np.ndarray, sat: float, con: float) -> np.ndarray:
    import cv2

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 1] = np.clip(hsv[..., 1] * sat, 0, 255)
    out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)
    return np.clip((out - 128) * con + 128, 0, 255).astype(np.uint8)


def _background(img: np.ndarray, kind: str, accent: str) -> np.ndarray:
    import cv2

    if kind == "blur-dark":
        return (cv2.GaussianBlur(img, (0, 0), 25) * 0.5).astype(np.uint8)
    if kind == "colour-field":
        b, g, r = np.median(img.reshape(-1, 3), axis=0)
        base = np.array([b, g, r], np.float32)
        acc = np.array(_hex(accent)[::-1], np.float32)
        ramp = np.linspace(0, 1, W, dtype=np.float32)[None, :, None]
        field = (0.35 * base + 0.25 * acc) * (1 - ramp) + 0.15 * acc * ramp
        return np.broadcast_to(field, (H, W, 3)).astype(np.uint8)
    return (img * 0.85).astype(np.uint8)


def _place_subject(bg: np.ndarray, img: np.ndarray, mask: np.ndarray, face, profile: dict,
                   side: str) -> tuple[np.ndarray, tuple | None]:
    """Scale the cutout so the face fills the profile's share of height, and put it on its side."""
    import cv2

    if face is None:
        scale, fx, fy = 1.0, W / 2, H / 2
    else:
        x, y, fw, fh = face[:4]
        scale = float(np.clip(profile["face_share"] * H / max(fh, 1), 0.6, 4.0))
        fx, fy = x + fw / 2, y + fh / 2
    big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    bm = cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    tx = {"left": profile["face_x"], "right": 1 - profile["face_x"], "centre": 0.5}[side] * W
    ox, oy = int(tx - fx * scale), int(H * 0.45 - fy * scale)
    # A person is cut off by the frame's bottom edge; keep that edge at the canvas bottom
    # so the subject never floats.
    if oy + big.shape[0] < H:
        oy = H - big.shape[0]
    out = bg.astype(np.float32).copy()
    x0, y0 = max(0, ox), max(0, oy)
    x1, y1 = min(W, ox + big.shape[1]), min(H, oy + big.shape[0])
    if x1 > x0 and y1 > y0:
        sub = big[y0 - oy:y1 - oy, x0 - ox:x1 - ox].astype(np.float32)
        a = bm[y0 - oy:y1 - oy, x0 - ox:x1 - ox][..., None]
        out[y0:y1, x0:x1] = sub * a + out[y0:y1, x0:x1] * (1 - a)
    face_box = None
    if face is not None:
        fw, fh = face[2] * scale, face[3] * scale
        face_box = (ox + fx * scale - fw / 2, oy + fy * scale - fh / 2, fw, fh)
    return out.astype(np.uint8), face_box


def _draw_text(img: np.ndarray, text: str, region: tuple, profile: dict, height_frac: float):
    """Bold text with a stroke, fitted inside region; returns the image and the text box."""
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = region
    words = text.upper().split()
    lines = [" ".join(words[:2]), " ".join(words[2:])] if len(words) > 2 else [" ".join(words)]
    lines = [ln for ln in lines if ln]
    size = int(height_frac * H)
    pil = Image.fromarray(img[..., ::-1])
    draw = ImageDraw.Draw(pil)
    while size > 20:
        font = _font(size)
        widths = [draw.textlength(ln, font=font) for ln in lines]
        if max(widths) <= (x1 - x0) and size * 1.1 * len(lines) <= (y1 - y0):
            break
        size -= 4
    font = _font(size)
    stroke = max(3, size // 12) if profile.get("outline", True) else 0
    total_h = size * 1.1 * len(lines)
    y = y0 + ((y1 - y0) - total_h) / 2
    box = [1e9, 1e9, 0, 0]
    for n, ln in enumerate(lines):
        tw = draw.textlength(ln, font=font)
        x = x0 + ((x1 - x0) - tw) / 2
        fill = _hex(profile["accent"]) if n == len(lines) - 1 and len(lines) > 1 \
            else _hex(profile["text_colour"])
        draw.text((x, y), ln, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
        bb = draw.textbbox((x, y), ln, font=font, stroke_width=stroke)
        box = [min(box[0], bb[0]), min(box[1], bb[1]), max(box[2], bb[2]), max(box[3], bb[3])]
        y += size * 1.1
    return np.array(pil)[..., ::-1].copy(), tuple(box), stroke


def _arrow(img: np.ndarray, start: tuple, end: tuple, colour: str) -> np.ndarray:
    import cv2

    c = _hex(colour)[::-1]
    out = img.copy()
    cv2.arrowedLine(out, tuple(map(int, start)), tuple(map(int, end)), (0, 0, 0), 26,
                    tipLength=0.3, line_type=cv2.LINE_AA)
    cv2.arrowedLine(out, tuple(map(int, start)), tuple(map(int, end)), c, 16, tipLength=0.3,
                    line_type=cv2.LINE_AA)
    return out


def compose(frame: np.ndarray, text: str, profile: dict, layout: str,
            second: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """One candidate thumbnail and the geometry the checks need."""
    import cv2

    graded = _grade(frame, profile["saturation"], profile["contrast"])
    faces = _faces_full(graded)
    face = max(faces, key=lambda f: f[2] * f[3]) if faces else None
    if layout == "split" and second is not None:
        a, b = graded, _grade(second, profile["saturation"], profile["contrast"])
        canvas = a.copy()
        poly = np.array([[W * 0.55, 0], [W, 0], [W, H], [W * 0.45, H]], np.int32)
        m = np.zeros((H, W), np.uint8)
        cv2.fillPoly(m, [poly], 1)
        canvas[m == 1] = b[m == 1]
        cv2.polylines(canvas, [poly[[0, 3]]], False, (255, 255, 255), 10, cv2.LINE_AA)
        img, tb1, stroke = _draw_text(canvas, "BEFORE", (40, 30, int(W * 0.45), 160),
                                      profile, 0.12)
        img, tb2, _ = _draw_text(img, "AFTER", (int(W * 0.58), 30, W - 40, 160),
                                 {**profile, "text_colour": profile["accent"]}, 0.12)
        box = (min(tb1[0], tb2[0]), min(tb1[1], tb2[1]), max(tb1[2], tb2[2]),
               max(tb1[3], tb2[3]))
        fb = None
        if face is not None:
            fb = tuple(face[:4])
        return img, {"text_box": box, "face_box": fb, "stroke": stroke, "layout": layout}

    mask, method = cutout_mask(graded) if profile.get("cutout", True) else \
        (np.ones(graded.shape[:2], np.float32), "none")
    bg = _background(graded, profile["background"], profile["accent"])
    side = {"face-left": "left", "face-right": "right", "hero-arrow": "centre"}.get(layout, "left")
    img, face_box = _place_subject(bg, graded, mask, face, profile, side)
    if side == "left":
        region = (int(W * 0.56), int(H * 0.08), W - 40, int(H * 0.72))
    elif side == "right":
        region = (40, int(H * 0.08), int(W * 0.44), int(H * 0.72))
    else:
        region = (40, 24, W - 40, int(H * 0.26))
    img, box, stroke = _draw_text(img, text, region, profile, profile["text_height"])
    if profile.get("arrow") and face_box is not None:
        fx, fy, fw, fh = face_box
        img = _arrow(img, ((box[0] + box[2]) / 2, box[3] + 10),
                     (fx + fw * 0.2, fy - 10 if fy > box[3] + 60 else fy + fh / 2),
                     profile["accent"])
    return img, {"text_box": box, "face_box": face_box, "stroke": stroke, "layout": layout,
                 "cutout": method}


# --- checks and ranking ----------------------------------------------------------------

def _luminance(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return c @ np.array([0.2126, 0.7152, 0.0722])


def _overlaps(a: tuple, b: tuple) -> bool:
    """Do two (x0, y0, x1, y1) rectangles intersect?"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def check(img: np.ndarray, geom: dict, profile: dict) -> dict:
    """The plan's three checks, at the 240 px the viewer actually sees."""
    import cv2

    s = CHECK_W / W
    small = cv2.resize(img, (CHECK_W, int(H * s)), interpolation=cv2.INTER_AREA)
    x0, y0, x1, y1 = [int(v * s) for v in geom["text_box"]]
    region = small[max(0, y0):y1 + 1, max(0, x0):x1 + 1][..., ::-1].reshape(-1, 3).astype(float)
    if len(region) < 4:
        ratio = 1.0
    else:
        lum = _luminance(region)
        # Text against what is right behind it: the brightest and darkest tenth of the
        # text box are glyph fill and stroke (or background) at this size.
        hi, lo = np.percentile(lum, 95), np.percentile(lum, 10)
        ratio = (hi + 0.05) / (lo + 0.05)
    face_px = 0
    if geom.get("face_box"):
        face_px = int(geom["face_box"][3] * s)
    # The timestamp badge covers the bottom-right corner: neither text nor face may sit there.
    corner = (CORNER[0] * W, CORNER[1] * H, W, H)
    boxes = [geom["text_box"]]
    if geom.get("face_box"):
        fx, fy, fw, fh = geom["face_box"]
        boxes.append((fx, fy, fx + fw, fy + fh))
    corner_clear = not any(_overlaps(bx, corner) for bx in boxes)
    face_needed = profile["layout"] != "split" and geom.get("face_box") is not None
    out = {"contrast_ratio": round(float(ratio), 1), "contrast_ok": bool(ratio >= 4.5),
           "face_px": int(face_px), "face_ok": bool(face_px >= 22) if face_needed else True,
           "corner_clear": bool(corner_clear)}
    out["passed"] = out["contrast_ok"] and out["face_ok"] and out["corner_clear"]
    return out


def hooks(title: str, beats: list[dict], sentences: dict, n: int = 5) -> list[str]:
    """2-4 word thumbnail text that complements the title rather than repeating it."""
    ep = llm.planner_endpoint()
    if ep is not None and llm.ping(ep):
        try:
            reply = llm.chat(ep, [{"role": "user", "content":
                                   f"Video title: {title}\nWrite {n} thumbnail texts of 2 to 4 "
                                   "words that complement the title without repeating its "
                                   "words. Punchy, no emojis, no clickbait the video does not "
                                   "pay off. One per line, nothing else."}],
                             max_tokens=120, temperature=0.8)
            lines = [re.sub(r"^[\d\.\-\*\s]+", "", ln).strip().strip('"')
                     for ln in (reply.get("content") or "").splitlines()]
            got = [ln for ln in lines if 2 <= len(ln.split()) <= 4]
            if got:
                return got[:n]
        except llm.LLMError:
            pass
    title_terms = {t for t in re.findall(r"[\w']+", title.lower())}
    emph = {w.lower() for b in beats for w in b.get("emphasis", [])}
    scored: list[tuple[float, str]] = []
    for b in beats:
        if b.get("role") == "cta":
            continue  # "thanks for watching" is never a thumbnail
        for sid in b.get("segment_ids", []):
            toks = re.findall(r"[\w']+", sentences.get(sid, {}).get("text", ""))
            for size in (2, 3, 4):
                for i in range(len(toks) - size + 1):
                    win = toks[i:i + size]
                    low = [t.lower() for t in win]
                    if low[0] in STOP or low[-1] in STOP or any(t in title_terms for t in low) \
                            or any(t in OUTRO for t in low):
                        continue
                    content = sum(t not in STOP for t in low) / size
                    score = content + 0.6 * any(t in emph for t in low) \
                        + 0.3 * any(t.isdigit() for t in low) + 0.05 * size \
                        + 0.4 * sentences.get(sid, {}).get("score", 0.5)
                    scored.append((score, " ".join(win)))
    out: list[str] = []
    for _, phrase in sorted(scored, key=lambda x: -x[0]):
        if all(not set(phrase.lower().split()) & set(o.lower().split()) for o in out):
            out.append(phrase)
        if len(out) >= n:
            break
    return out or ["WATCH THIS"]


def _distance(feats: dict, profile: dict) -> float:
    d = [abs(feats["face_share"] - profile["face_share"]) / 0.3 if feats["face"] else 1.0,
         abs(feats["words"] - profile["words"]) / 3,
         abs(feats["text_height"] - profile["text_height"]) / 0.1]
    return float(np.mean(d))


def build(project, master: Path, words: list[dict], title: str, beats: list[dict],
          sentences: dict, profile_id: str | None, log: Log = print,
          timeline_path: Path | None = None) -> list[dict]:
    import cv2

    profile = get_profile(profile_id)
    frames = mine_frames(master, words, title)
    if not frames:
        return []
    texts = hooks(title, beats, sentences)
    layouts = {"face-side": ["face-left", "face-right"], "hero-arrow": ["hero-arrow"],
               "split": ["split"]}[profile["layout"]]
    out_dir = project.path("package", "thumbnails")
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = []
    def clean(t: float) -> np.ndarray:
        if timeline_path is None:
            return grab(master, t)
        src, st = source_at(project, timeline_path, t)
        return grab(src, st)

    for k, fr in enumerate(frames[:6]):
        img = clean(fr["t"])
        second = clean(frames[-1 - k]["t"]) if profile["layout"] == "split" else None
        for attempt in range(4):
            layout = layouts[(k + attempt) % len(layouts)]
            text = texts[(k + attempt) % len(texts)]
            prof = dict(profile)
            if attempt >= 2:
                prof["text_height"] = profile["text_height"] * 0.8  # recompose smaller
            thumb, geom = compose(img, text, prof, layout, second)
            checks = check(thumb, geom, prof)
            if checks["passed"] or attempt == 3:
                feats = {"face": geom.get("face_box") is not None,
                         "face_share": (geom["face_box"][3] / H) if geom.get("face_box") else 0,
                         "words": len(text.split()),
                         "text_height": (geom["text_box"][3] - geom["text_box"][1]) / H /
                         max(1, min(2, len(text.split()) // 2 + 1))}
                score = round((1 - min(1.0, _distance(feats, profile))) * 0.6
                              + 0.4 * fr["score"] + (0.5 if checks["passed"] else 0), 3)
                candidates.append({"img": thumb, "text": text.upper(), "layout": layout,
                                   "frame_t": fr["t"], "checks": checks, "score": score,
                                   "cutout": geom.get("cutout")})
                break
    candidates.sort(key=lambda c: -c["score"])
    picked, used_t = [], set()
    for c in candidates:
        if c["frame_t"] in used_t:
            continue
        picked.append(c)
        used_t.add(c["frame_t"])
        if len(picked) == 3:
            break
    result = []
    for rank, c in enumerate(picked, 1):
        path = out_dir / f"thumb_{rank}.png"
        cv2.imwrite(str(path), c["img"])
        result.append({"path": str(path.relative_to(project.root)), "rank": rank,
                       "score": float(c["score"]), "text": c["text"], "layout": c["layout"],
                       "frame_t": float(c["frame_t"]), "checks": c["checks"],
                       "cutout": c["cutout"]})
    log(f"thumbnails: {sum(r['checks']['passed'] for r in result)}/{len(result)} pass the checks")
    return result
