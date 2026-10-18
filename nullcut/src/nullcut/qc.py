"""Quality checks: Gate 1 (plan §13) and the style match score (plan §5).

Gate 1: length within ±5 %, no cut mid-word, −14 LUFS ±1 dB. The style
match here is computed from the timeline the compiler produced. Phase 2
replaces it with the same measurements run on the rendered pixels.
"""

from __future__ import annotations

from . import captions, config


def _match(actual: float, wanted: float) -> float:
    if wanted <= 0:
        return 1.0 if actual <= 0.05 else 0.0
    return round(max(0.0, 1.0 - abs(actual - wanted) / wanted), 2)


def style_match(report: dict, style: dict, caption_blocks: list[int] | None,
                captions_on: bool, has_music: bool, music_wanted: bool) -> dict:
    out = {
        "pacing": _match(report["cuts_per_min"], style["pacing"]["cuts_per_min"]),
        "zoom": _match(report["zooms_per_min"], style["zoom"]["per_minute"]),
        "broll": _match(report["broll_share"], style["broll"]["density"]),
    }
    if captions_on and caption_blocks:
        mean = sum(caption_blocks) / len(caption_blocks)
        out["captions"] = 1.0 if mean <= style["captions"]["max_words"] + 0.01 else \
            _match(mean, style["captions"]["max_words"])
    if music_wanted:
        out["music"] = 1.0 if has_music else 0.0
    return out


def gate1(render: dict, target_s: float | None, tolerance: float) -> dict:
    lufs = render.get("integrated_lufs")
    length = render.get("video_s")
    return {
        "length_ok": (abs(length - target_s) <= target_s * tolerance)
        if target_s and length else None,
        "loudness_lufs": lufs,
        "loudness_ok": lufs is not None and abs(lufs - config.LOUDNESS_TARGET_LUFS) <= 1.0,
        "true_peak_dbtp": render.get("true_peak_dbtp"),
        "cuts_clean": render.get("cuts_clean", True),
        "worst_cut_db": render.get("worst_cut_db"),
    }


def caption_block_sizes(words: list[dict], style: str, max_words: int) -> list[int]:
    return [len(b) for b in captions.group(words, style, max_words)]
