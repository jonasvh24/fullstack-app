"""Defaults for Phase 0.

Everything a style profile will later own lives here as plain constants, so
Phase 2 can replace this module's values with a measured profile without
touching the compiler.
"""

from __future__ import annotations

import os
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".webm", ".mxf",
}

# Mix audio is kept as raw PCM so the renderer can memory-map slices instead of
# decoding a whole clip per cut.
MIX_SAMPLE_RATE = 48_000
MIX_CHANNELS = 2
ASR_SAMPLE_RATE = 16_000

PROXY_HEIGHT = 720

# Jump-cut rule. A gap between kept words longer than this is removed.
SILENCE_THRESHOLD_S = 0.35
# When the cut is over target, the threshold tightens through these steps
# before any material is dropped (plan §6: tighten first, then drop).
SILENCE_TIGHTEN_STEPS_S = (0.30, 0.25, 0.20, 0.15)
# Handles keep plosives and breath tails from being clipped at a cut.
HANDLE_PRE_S = 0.08
HANDLE_POST_S = 0.12
# Spans shorter than this read as a stutter rather than an edit.
MIN_SPAN_S = 0.25
TARGET_TOLERANCE = 0.05

# Sentence units: the smallest piece the duration fit is allowed to drop.
SEGMENT_MAX_S = 15.0
SEGMENT_BREAK_GAP_S = 1.0

# Hard fillers are removed wherever they occur.
HARD_FILLERS = {
    "en": {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "hmm", "mm", "mhm"},
    "nl": {"euh", "eh", "ehm", "uhm", "uh", "hm", "hmm", "mm"},
}
# Soft fillers are real words ("dus" means "so"). Removing every instance
# would mangle Dutch, so they go only when isolated by pauses or when they
# open a sentence followed by a pause.
SOFT_FILLERS = {
    "en": {"so", "like", "basically", "okay", "ok"},
    "nl": {"dus", "nou", "zeg", "ja", "oké"},
}
SOFT_FILLER_PAUSE_S = 0.20

# Phase 0 highlight score: hand-set weights, learned per user from Phase 3.
HIGHLIGHT_WEIGHTS = {"speech_rate": 0.30, "confidence": 0.20, "energy": 0.25,
                     "face": 0.10, "sharp": 0.10, "steady": 0.05}

# Output
DEFAULT_FPS = 30.0
MASTER_SIZE = (1920, 1080)
PREVIEW_SIZE = (1280, 720)
LOUDNESS_TARGET_LUFS = -14.0
TRUE_PEAK_DBTP = -1.0
LOUDNESS_RANGE = 11.0
EDGE_FADE_S = 0.008  # removes clicks at jump cuts without audible dips
AUDIO_BITRATE = "192k"

# Captions
CAPTION_FONT = os.environ.get("NULLCUT_CAPTION_FONT", "DejaVu Sans")
CAPTION_MAX_WORDS = {"clean": 6, "word": 3}
CAPTION_MAX_CHARS = {"clean": 32, "word": 18}
CAPTION_MAX_S = 2.5
CAPTION_BREAK_GAP_S = 0.45
CAPTION_HIGHLIGHT = "&H0000D7FF&"  # ASS is BGR: this is amber


def nullcut_home(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("NULLCUT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / "nullcut").resolve()
