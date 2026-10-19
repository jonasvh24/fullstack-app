"""Phase 2: measurement, extraction, match, comparison, reframe, thumbnails."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from nullcut import (compiler, composer, measure, package, render, style_engine, styles,
                     thumbnails)
from nullcut.compiler import CutOptions, Item, compile_rough

from .conftest import make_clip_record, needs_ffmpeg


def _video(path: Path, filter_graph: str, seconds: float = 4.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=640x360:r=25:d={seconds}", "-filter_complex", filter_graph,
                    "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path


@needs_ffmpeg
def test_cut_detector_sees_a_jump_cut_but_not_a_zoom(tmp_path):
    # A jump cut: the same picture, shifted 40 px at 2.0 s.
    jump = _video(tmp_path / "jump.mp4",
                  "[0]crop=560:320:'if(lt(t,2),20,60)':20,scale=640:360[v]", 4.0)
    cuts = measure.detect_cuts(jump, 25.0)
    assert len(cuts) == 1 and abs(cuts[0] - 2.0) <= 0.08
    # An eased zoom-in from 1.0 to 1.2 over 0.3 s at 2.0 s: motion, not a cut.
    zoom = _video(tmp_path / "zoom.mp4",
                  "[0]scale=w='640*(1+0.2*min(max((t-2)/0.3,0),1))':h=-2:eval=frame,"
                  "crop=640:360[v]", 4.0)
    assert measure.detect_cuts(zoom, 25.0) == []


def test_zoom_events_need_a_real_scale_change():
    steps = [(t / 10, 0.0) for t in range(10)]
    steps += [(1.0 + k / 10, 0.025) for k in range(4)]          # +10 % over 0.4 s
    steps += [(1.5 + t / 10, 0.001) for t in range(10)]         # handheld wobble
    events = measure.zoom_events(steps)
    assert len(events) == 1 and events[0]["scale"] == pytest.approx(np.exp(0.1), abs=0.01)
    assert measure.zoom_events([(t / 10, 0.002) for t in range(10)]) == []


def test_music_level_reads_gaps_and_ducked_pauses():
    rate = 16_000
    t = np.arange(rate * 12) / rate
    music = 0.05 * np.sin(2 * np.pi * 110 * t)
    speech = np.zeros_like(t)
    words = []
    for a in (1.0, 2.0, 2.9, 8.0):
        words.append({"w": "x", "s": a, "e": a + 0.5})
        speech[int(a * rate):int((a + 0.5) * rate)] = 0.5 * np.sin(
            2 * np.pi * 300 * t[int(a * rate):int((a + 0.5) * rate)])
    duck = np.ones_like(t)
    duck[int(0.8 * rate):int(4.0 * rate)] = 0.25                 # ducked by ~12 dB
    m = measure.music_stats((speech + music * duck).astype(np.float32), rate, words, 12.0)
    assert m["presence"] is True
    assert m["gap_db"] == pytest.approx(-20, abs=2)              # 0.05 vs 0.5 -> -20 dB
    assert m["under_speech_db"] == pytest.approx(-32, abs=2.5)   # ducked a further 12 dB


def _measurement(**over) -> dict:
    m = {"file": "ref.mp4", "duration": 60.0,
         "pacing": {"cuts_per_min": 20.0, "hook_cuts_per_min": 24.0, "shot_median": 2.5,
                    "shot_p10": 1.0, "shot_p90": 5.0},
         "silence_ms": 210, "zoom": {"per_minute": 9.0, "scale_median": 1.2, "rise_s": 0.3},
         "max_static_s": 3.0,
         "captions": {"present": True, "words_per_block": 3.0, "position_y": 0.85,
                      "upper_ratio": 0.95, "highlight": "#00E0F0"},
         "overlays_per_min": 0.0, "broll_ratio": 0.2,
         "music": {"presence": True, "gap_db": -8.0, "under_speech_db": -19.0,
                   "cuts_on_beat": 0.1},
         "sfx_cut_share": 0.4,
         "colour": {"saturation": 0.5, "contrast": 0.2, "warmth": 0.1, "lift": 0.05}}
    m.update(over)
    return m


def test_extraction_maps_measurements_to_a_profile():
    p = style_engine.profile_from_measurements("Fast one", [_measurement(), _measurement(),
                                                            _measurement(silence_ms=250)])
    assert p["id"] == "fast-one" and p["kind"] == "extracted" and len(p["sources"]) == 3
    assert p["pacing"]["silence_ms"] == 210 and p["pacing"]["cuts_per_min"] == 20.0
    assert p["captions"]["mode"] == "word" and p["captions"]["case"] == "upper"
    assert p["captions"]["highlight"] == "#00E0F0" and p["captions"]["emphasis"]
    assert p["zoom"]["per_minute"] == 9.0 and p["zoom"]["scale_max"] == 1.2
    assert p["music"]["under_speech_db"] == -19.0 and p["music"]["beat_snap"] is False
    assert p["colour"] == {"target": {"saturation": 0.5, "contrast": 0.2, "warmth": 0.1,
                                      "lift": 0.05}}
    assert p["inherited_from"] in styles.all_styles()
    styles._validate(p, "extracted")  # loadable like a shipped style


def test_extraction_turns_captions_off_and_snaps_when_cuts_ride_the_beat():
    no_caps = _measurement(captions={"present": False},
                           music={"presence": True, "gap_db": -6, "under_speech_db": -14,
                                  "cuts_on_beat": 0.7})
    p = style_engine.profile_from_measurements("Montage", [no_caps] * 3)
    assert p["captions"]["mode"] == "off" and p["music"]["beat_snap"] is True


def test_match_names_the_rule_that_missed():
    style = styles.get("retention-max")
    good = style_engine.match(_measurement(**{
        "pacing": {"cuts_per_min": 24.0}, "silence_ms": 250, "max_static_s": 2.5,
        "zoom": {"per_minute": 10.0}, "broll_ratio": 0.25, "sfx_cut_share": 0.25,
        "captions": {"present": True, "words_per_block": 3.0, "upper_ratio": 1.0,
                     "highlight": "#FFC400"},
        "music": {"presence": True, "under_speech_db": -20.0}}), style, True, "word", None)
    assert style_engine.overall(good) >= 0.95 and not any(d["note"] for d in good.values())
    bad = style_engine.match(_measurement(**{
        "pacing": {"cuts_per_min": 6.0}, "zoom": {"per_minute": 0.0}, "max_static_s": 9.0,
        "music": {"presence": False}}), style, True, "word", None)
    assert bad["pacing"]["note"].startswith("rule 1")
    assert bad["zoom"]["note"].startswith("rule 4")
    assert bad["static"]["note"].startswith("rule 3")
    assert bad["music"]["note"].startswith("rule 7")
    unknown = style_engine.match(_measurement(music={"presence": None}), style, True, "word")
    assert "music" not in unknown  # no pause to listen into: unknown, not a miss


def test_compare_puts_every_style_on_the_same_axes():
    rows = style_engine.compare(["explainer", "retention-max", "documentary"])
    pace = next(r for r in rows if r["key"] == "pacing.cuts_per_min")
    assert pace["values"] == {"explainer": 8, "retention-max": 24, "documentary": 5}
    assert pace["min"] < 5 < 24 < pace["max"]
    with pytest.raises(ValueError):
        style_engine.compare(["explainer"])
    with pytest.raises(KeyError):
        style_engine.compare(["explainer", "nope"])


def test_six_shipped_styles_and_user_styles_load(tmp_path):
    shipped = {k for k, v in styles.all_styles().items() if v["kind"] == "shipped"}
    assert shipped == {"explainer", "retention-max", "cinematic-vlog", "podcast-clip",
                       "documentary", "tutorial"}
    p = style_engine.profile_from_measurements("Mine", [_measurement()])
    styles.user_dir().mkdir(parents=True, exist_ok=True)
    (styles.user_dir() / "mine.json").write_text(json.dumps(p))
    (styles.user_dir() / "broken.json").write_text("{not json")
    assert styles.all_styles()["mine"]["kind"] == "extracted"
    styles.delete("mine")
    with pytest.raises(ValueError):
        styles.delete("explainer")


def test_colour_transfer_moves_footage_toward_the_target():
    src = {"saturation": 0.4, "contrast": 0.2, "warmth": 0.0, "lift": 0.05}
    tgt = {"saturation": 0.5, "contrast": 0.22, "warmth": 0.1, "lift": 0.05}
    from nullcut import fx
    params = fx.colour_transfer(tgt, src)
    reach = style_engine.expected_colour(params, src)
    for k in ("saturation", "contrast", "warmth"):
        assert reach[k] == pytest.approx(tgt[k], abs=0.01)


def test_calm_styles_keep_pauses_between_sentences():
    script = [("One", 0.5, 0.8), ("two.", 0.85, 1.2), ("Three", 1.55, 1.9), ("four.", 1.95, 2.3)]
    rec = make_clip_record("c_s", script, 3.0)   # a 0.35 s pause between the sentences
    idx = {"clips": [rec]}
    calm = compile_rough(idx, CutOptions(silence_s=0.42))
    fast = compile_rough(idx, CutOptions(silence_s=0.15))
    assert calm.report["jump_cuts"] == 0           # the pause plays whole
    assert fast.report["jump_cuts"] == 1
    assert compiler.min_cut_gap_s() == pytest.approx(0.25)


def test_subject_track_follows_with_a_dead_zone():
    clip = {"faces": [[t / 5, 0.1 + (0.5 if t >= 10 else 0.0), 0.3, 0.1, 0.2, 0.9]
                      for t in range(30)]}
    track = composer.subject_track(clip, 0.0, 6.0)
    xs = [c for _, c in track]
    assert xs[0] == pytest.approx(0.15, abs=0.01)
    assert xs[-1] > 0.55                              # it got there
    assert all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))   # and never jittered back
    steady = composer.subject_track({"faces": [[t / 5, 0.48 + 0.02 * (t % 2), 0.3, 0.04, 0.2,
                                                0.9] for t in range(30)]}, 0.0, 6.0)
    assert len({c for _, c in steady}) == 1           # small moves never move the frame


@needs_ffmpeg
def test_vertical_reframe_follows_the_subject(tmp_path):
    src = tmp_path / "src.mp4"
    # A white square at the right of a dark 16:9 frame.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=1280x720:r=25:d=2", "-vf",
                    "drawbox=x=960:y=300:w=120:h=120:color=white:t=fill",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    out = tmp_path / "v.mp4"
    render.render_item(src, Item("c", "s", 0.0, 1.0, 25), out, None, (360, 640), "master",
                       25.0, reframe=[[0.0, 0.797], [1.0, 0.797]])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(out), "-frames:v",
                          "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(640, 360)
    _, xs = np.nonzero(frame > 128)
    assert len(xs) and abs(xs.mean() - 180) < 20     # the square sits in the middle


def test_thumbnail_checks_fail_what_they_should():
    img = np.full((thumbnails.H, thumbnails.W, 3), 128, np.uint8)
    profile = thumbnails.get_profile("reaction-face")
    # Grey text on a grey field: unreadable.
    geom = {"text_box": (700, 100, 1200, 300), "face_box": (100, 100, 300, 300)}
    assert not thumbnails.check(img, geom, profile)["contrast_ok"]
    img[110:290, 710:1190] = 255
    img[180:220, 710:1190] = 0
    good = thumbnails.check(img, geom, profile)
    assert good["contrast_ok"] and good["face_ok"] and good["corner_clear"]
    # A face under the timestamp badge.
    under = thumbnails.check(img, {**geom, "face_box": (1100, 620, 100, 90)}, profile)
    assert not under["corner_clear"] and not under["passed"]
    tiny = thumbnails.check(img, {**geom, "face_box": (100, 100, 40, 40)}, profile)
    assert not tiny["face_ok"]


def test_hooks_complement_the_title_and_skip_the_outro():
    sentences = {"a": {"text": "This runs entirely on local hardware.", "score": 0.9},
                 "b": {"text": "Thanks for watching.", "score": 0.95}}
    beats = [{"role": "hook", "segment_ids": ["a"], "emphasis": ["entirely"]},
             {"role": "cta", "segment_ids": ["b"]}]
    out = thumbnails.hooks("Local AI editing", beats, sentences)
    assert out and all("watching" not in h.lower() for h in out)
    assert all("local" not in h.lower() for h in out)   # the title's words stay in the title
    assert all(2 <= len(h.split()) <= 4 for h in out)


def test_title_pattern_and_case():
    pat = package.title_pattern("I Tried 5 Editors For 30 Days\nWhy Nobody Talks About This\n"
                                "The Only Setup You Need\nIs Local AI Worth It?")
    assert pat["caps"] == "title" and pat["numbers"] == 0.25 and pat["questions"] == 0.25
    assert package.title_pattern("one\ntwo") is None
    assert package.apply_caps("that is the whole trick", "title") == "That Is The Whole Trick"
