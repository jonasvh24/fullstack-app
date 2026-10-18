"""Compiler rules (plan §6), music fitting (§8) and the zoom renderer (§7)."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from nullcut import composer, music, plan, render, vision
from nullcut.compiler import Item

from .conftest import make_music, needs_ffmpeg

pytestmark = needs_ffmpeg


def compose(project, style, target=None, captions="word", music_mode="none", library=None):
    plan.save_brief(project, {"style": style, "target_s": target, "captions": {"mode": captions},
                              "music": {"mode": music_mode, "mood": "", "energy": "high",
                                        "track_id": None}})
    doc = plan.make_plan(project, log=lambda m: None)
    return composer.compose(project.clip_index(), doc, plan.load_brief(project), 30.0, library)


def test_vision_finds_the_three_broll_shots_and_keyframes(project):
    broll = next(c for c in project.clip_index()["clips"] if not c["words"])
    assert [(round(s["start"], 1), round(s["end"], 1)) for s in broll["shots"]] == \
           [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]
    assert all(project.path(s["keyframe"]).exists() for s in broll["shots"])
    assert all("score" in s for s in broll["shots"])
    speech = next(c for c in project.clip_index()["clips"] if c["words"])
    assert all(project.path(s["keyframe"]).exists() for s in speech["segments"])
    assert len(speech["quality"]["sharp"]) == int(np.ceil(speech["duration"]))


def test_zooms_land_on_emphasis_words_inside_their_item(project):
    c = compose(project, "retention-max")
    assert c.zooms, "retention max must punch"
    starts = composer._starts(c.items, 30.0)
    for z in c.zooms:
        k = max(i for i, s in enumerate(starts) if s <= z["start_s"])
        a, b = starts[k], starts[k] + c.items[k].frames / 30.0
        assert a + composer.ITEM_EDGE_GUARD_S - 1e-6 <= z["start_s"] < z["end_s"] <= b
        assert 1.0 < z["scale"] <= 1.25 and z["rule"]


def test_max_static_is_enforced_with_zoom_or_broll(project):
    c = compose(project, "retention-max")
    events = sorted({0.0, c.report["duration_s"], *composer._cuts(c.items, 30.0),
                     *[x for z in c.zooms for x in (z["start_s"], z["end_s"])],
                     *[x for b in c.broll for x in (b["start_s"], b["end_s"])]})
    gaps = [b - a for a, b in zip(events, events[1:])]
    # The rule may give up only where nothing fits; on this footage everything fits.
    assert max(gaps) <= c.report["max_static_s"] + 1e-6


def test_broll_stays_off_the_hook_opening_and_inside_the_edit(project):
    c = compose(project, "cinematic-vlog")
    hook = c.beats[0]
    assert c.broll
    for b in c.broll:
        assert b["start_s"] >= hook["start_s"] + composer.HOOK_FACE_S
        assert b["end_s"] <= c.report["duration_s"]
        assert b["shot_id"].split(":")[0] == b["clip_id"]


def test_music_led_cuts_snap_onto_the_beat(project, monkeypatch, tmp_path):
    make_music(tmp_path / "lib" / "pulse.wav")
    library = music.import_folder(tmp_path / "lib", log=lambda m: None)
    assert abs(library[0]["bpm"] - 120) / 120 < 0.05
    c = compose(project, "cinematic-vlog", music_mode="library", library=library)
    beats = composer._music_beats_out(library[0], c.music["pieces"])
    starts = composer._starts(c.items, 30.0)
    snapped = [starts[k + 1] for k, it in enumerate(c.items[:-1])
               if any("beat snap" in r for r in it.rules)]
    assert snapped, "at least one cut should be within ±120 ms of a beat"
    for t in snapped:
        assert min(abs(b - t) for b in beats) <= 0.5 / 30 + 1e-6


def test_music_fit_ends_on_the_final_downbeat():
    track = {"duration": 60.0, "downbeats": [2.0 * i for i in range(29)]}
    for d in (20.0, 56.0, 90.0, 200.0):
        pieces = music.fit_plan(track, d)
        assert sum(b - a for a, b in pieces) == pytest.approx(d)
        assert all(b == 56.0 for _, b in pieces)


def test_ducking_sits_at_the_style_depths():
    sr = 48_000
    env = music.gain_envelope(sr * 10, speech=[(2.0, 6.0)], beat_energy=[(0, 10, "high")],
                              under_db=-20, gap_db=-8, fade_in_s=0.01)
    db = 20 * np.log10(env[:, 0])
    assert db[int(4.0 * sr)] == pytest.approx(-20, abs=0.5)
    assert db[int(9.0 * sr)] == pytest.approx(-8, abs=0.5)


def test_sfx_and_captions_follow_the_profile(project):
    c = compose(project, "retention-max")
    assert {s["kind"] for s in c.sfx} <= {"whoosh", "pop"} and c.sfx
    assert c.captions == {"enabled": True, "style": "word", "max_words": 3, "case": "upper",
                          "highlight": "#FFC400", "emphasis": True}
    quiet = compose(project, "explainer", captions="off")
    assert quiet.captions["enabled"] is False and not quiet.sfx or \
        all(s["kind"] == "pop" for s in quiet.sfx)


def _frame(path: Path, w: int, h: int, t: float = 0.5) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v",
                          "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w).astype(np.float32)


def test_perspective_zoom_matches_a_reference_crop(tmp_path):
    """A 2x zoom on the centre must equal the centre half of the frame, scaled up."""
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=320x180:r=30:d=2", "-c:v", "libx264", "-qp", "0",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    item = Item("c", "s", 0.0, 1.0, 30)
    zoom = {"start": 0.0, "end": 1.0, "scale": 2.0, "cx": 0.5, "cy": 0.5, "ease_s": 1.0,
            "kind": "drift"}
    out = tmp_path / "zoom.mp4"
    # Hold the zoom at 2x by evaluating at the end of a drift that has completed.
    zoom_hold = {**zoom, "end": 0.001, "ease_s": 0.001}
    render.render_item(src, item, out, None, (320, 180), "master", 30.0, [zoom_hold])
    ref = tmp_path / "ref.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                    "crop=160:90:80:45,scale=320:180:flags=bicubic", "-t", "1",
                    "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", str(ref)], check=True)
    a, b = _frame(out, 320, 180), _frame(ref, 320, 180)
    corr = np.corrcoef(a.ravel(), b.ravel())[0, 1]
    assert corr > 0.97, corr


def test_broll_overlay_replaces_the_picture_inside_its_window(tmp_path):
    base = tmp_path / "base.mp4"
    ins = tmp_path / "ins.mp4"
    for path, colour in ((base, "black"), (ins, "white")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={colour}:s=160x90:r=30:d=3", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(path)], check=True)
    out = tmp_path / "out.mp4"
    render.render_item(base, Item("c", "s", 0.0, 2.0, 60), out, None, (160, 90), "master", 30.0,
                       brolls=[{"start": 0.5, "end": 1.5, "src_in": 0.0, "src": str(ins)}])
    assert _frame(out, 160, 90, 0.2).mean() < 30
    assert _frame(out, 160, 90, 1.0).mean() > 220
    assert _frame(out, 160, 90, 1.8).mean() < 30


def test_nearest_keyframe_accepts_json_and_int_keys():
    assert vision.nearest_keyframe({"0": "a", "2000": "b"}, 1.4) == (2000, "b")
    assert vision.nearest_keyframe({0: "a", 2000: "b"}, 0.4) == (0, "a")
    assert vision.nearest_keyframe({}, 1.0) is None
