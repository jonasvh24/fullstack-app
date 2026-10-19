"""Phase 3: learned highlight weights, generated music, exports for Resolve."""

import json
import os
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from nullcut import composer, config, export, learn, musicgen, pipeline, plan, timeline

from .conftest import needs_ffmpeg

# --- learned weights ---------------------------------------------------------------------


def _write_examples(rows):
    learn.examples_path().parent.mkdir(parents=True, exist_ok=True)
    with learn.examples_path().open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _creator(n_projects=4, per=30, seed=0, feature="energy"):
    """A creator who keeps loud sentences and deletes quiet ones, whatever else they score."""
    rng = np.random.default_rng(seed)
    k = learn.FEATURES.index(feature)
    rows = []
    for p in range(n_projects):
        for i in range(per):
            x = rng.random(len(learn.FEATURES)).round(3).tolist()
            rows.append({"at": "2026-10-01T00:00:00+00:00", "project": f"p{p}",
                         "segment": f"s{i}", "label": int(x[k] > 0.5), "implicit": i % 3 == 0,
                         "source": "refine", "x": x})
    return rows


def test_refit_learns_what_this_creator_keeps():
    _write_examples(_creator())
    status = learn.refit()
    assert status["adopted"], status
    coef = status["coef"]
    # Energy dominates after the fit; the hand prior gave speech rate the most weight.
    assert max(coef, key=coef.get) == "energy"
    assert status["cv"]["learned_accuracy"] > status["cv"]["hand_accuracy"]
    assert learn.status()["active"] == "learned"


def test_too_few_choices_keep_the_hand_weights():
    _write_examples(_creator(n_projects=1, per=10))
    status = learn.refit()
    assert not status["adopted"] and "needs" in status["reason"]
    assert learn.current() is None


def test_a_refit_that_does_not_predict_better_is_not_adopted():
    rows = _creator()
    rng = np.random.default_rng(5)
    for r in rows:
        r["label"] = int(rng.random() < 0.5)   # coin flips: nothing to learn
    _write_examples(rows)
    assert not learn.refit()["adopted"]


def test_adopted_weights_rescore_every_project_on_load(project):
    before = {s["id"]: s["score"] for c in project.clip_index()["clips"] for s in c["segments"]}
    _write_examples(_creator())
    learn.refit()
    index = project.clip_index()
    after = {s["id"]: s for c in index["clips"] for s in c["segments"]}
    assert index["scored_with"] == learn.current()["id"]
    assert all(after[s]["score_hand"] == before[s] for s in before)
    feats = learn.ranks(index)
    k = learn.FEATURES.index("energy")
    loudest = max(after, key=lambda s: feats[s][k])
    quietest = min(after, key=lambda s: feats[s][k])
    assert after[loudest]["score"] > after[quietest]["score"]


def test_deletes_and_restores_become_labels(project):
    pool = list(plan.segment_pool(project.clip_index()))
    before = [{"segment_ids": pool[:3], "dropped_ids": [pool[3]]}]
    after = [{"segment_ids": [pool[0], pool[1], pool[3]], "dropped_ids": []}]
    n = learn.from_plans(project.id, project.clip_index(), before, after, "storyboard")
    rows = {r["segment"]: r for r in learn.load_examples()}
    assert n == len(rows)
    assert rows[pool[2]]["label"] == 0 and not rows[pool[2]]["implicit"]
    assert rows[pool[3]]["label"] == 1 and not rows[pool[3]]["implicit"]
    assert all(r["implicit"] for s, r in rows.items() if s not in (pool[2], pool[3]))


def test_nightly_refit_time(monkeypatch):
    monkeypatch.setenv("NULLCUT_REFIT_HOUR", "3")
    now = datetime(2026, 10, 19, 14, 30).astimezone()
    nxt = learn.next_refit(now)
    assert (nxt.hour, nxt.day) == (3, 20)
    assert learn.next_refit(now.replace(hour=1)).day == 19
    monkeypatch.setenv("NULLCUT_REFIT_HOUR", "off")
    assert learn.refit_hour() is None and learn.status()["next_refit"] is None


# --- generated music ------------------------------------------------------------------------

def _rms(x):
    return float(np.sqrt(np.mean(x * x)))


def test_builtin_bed_has_the_exact_length_grid_and_sections():
    sp = musicgen.spec({"mood": "", "energy": "mid"}, {"music": {"energy": "mid"}}, "seed")
    grid = musicgen.grid_track(sp, 30.0)
    assert grid["downbeats"][-1] == pytest.approx(30.0)           # last bar ends on the end
    spb = 60 / sp["bpm"]
    assert np.allclose(np.diff(grid["beats"]), spb, atol=1e-3)
    track = musicgen.builtin_track(sp, 30.0, [[0, 10, "low"], [10, 20, "high"], [20, 30, "low"]])
    with wave.open(track["path"]) as w:
        assert w.getnframes() == round(30.0 * config.MIX_SAMPLE_RATE)
        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2) / 32768
    sr = config.MIX_SAMPLE_RATE
    assert np.abs(audio).max() <= 10 ** (-1 / 20) + 1e-3          # peak at -1 dBFS
    assert _rms(audio[12 * sr:18 * sr]) > 1.5 * _rms(audio[2 * sr:8 * sr])   # high > low
    again = musicgen.builtin_track(sp, 30.0, [[0, 10, "low"], [10, 20, "high"],
                                              [20, 30, "low"]])
    assert again["path"] == track["path"]                         # cached per exact edit


@needs_ffmpeg
def test_generated_music_follows_the_edit_and_records_its_licence(project):
    plan.save_brief(project, {"style": "cinematic-vlog", "target_s": None,
                              "music": {"mode": "generate", "mood": "lofi", "energy": "mid",
                                        "track_id": None}})
    doc = plan.make_plan(project, log=lambda m: None)
    comp = composer.compose(project.clip_index(), doc, plan.load_brief(project), 30.0)
    m = comp.music
    duration = sum(it.frames for it in comp.items) / 30.0
    assert m["generated"] and Path(m["path"]).exists()
    assert m["pieces"] == [(0.0, pytest.approx(duration))]
    assert "CC0" in m["licence"] and m["bpm"] == 80
    # A music-led style snapped its cuts onto the generated grid.
    if comp.report["beat_snaps"]:
        grid = musicgen.grid_track(musicgen.spec(plan.load_brief(project)["music"],
                                                 {"music": {"energy": "mid"}},
                                                 [c["id"] for c in project.clip_index()["clips"]]),
                                   duration)
        starts = np.cumsum([0] + [it.frames for it in comp.items])[1:-1] / 30.0
        snapped = [t for t in starts if min(abs(t - b) for b in grid["beats"]) < 0.02]
        assert snapped


@needs_ffmpeg
def test_external_generator_runs_from_its_template(tmp_path, monkeypatch):
    script = tmp_path / "gen.py"
    script.write_text(
        "import sys, wave, numpy as np\n"
        "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
        "sr = 48000; n = int(float(args['--seconds']) * sr)\n"
        "t = np.arange(n) / sr; bpm = float(args['--bpm'])\n"
        "y = 0.3 * np.sin(2 * np.pi * 110 * t) * (np.mod(t, 60 / bpm) < 0.1)\n"
        "pcm = (np.stack([y, y], 1) * 32767).astype('<i2')\n"
        "w = wave.open(args['--out'], 'wb'); w.setnchannels(2); w.setsampwidth(2)\n"
        "w.setframerate(sr); w.writeframes(pcm.tobytes()); w.close()\n")
    monkeypatch.setenv("NULLCUT_MUSICGEN_CMD", f"{sys.executable} {script} --prompt {{prompt}} "
                                               "--seconds {seconds} --bpm {bpm} --out {out}")
    monkeypatch.setenv("NULLCUT_MUSICGEN_LICENCE", "ACE-Step output, Apache-2.0")
    sp = musicgen.spec({"mood": "upbeat", "energy": "high"}, {"music": {"energy": "mid"}}, "x")
    assert sp["backend"] == "command"
    track = musicgen.command_track(sp, 20.0, log=lambda m: None)
    assert track["duration"] >= 28 and track["licence"] == "ACE-Step output, Apache-2.0"
    assert track["beats"] and Path(track["path"]).exists()
    os.environ.pop("NULLCUT_MUSICGEN_CMD", None)


# --- exports --------------------------------------------------------------------------------

def test_fcpxml_clock_is_rational_and_frame_exact():
    c = export.Clock(29.97)
    assert c.frame_duration() == "1001/30000s"
    assert c.t(30) == "1001/1000s" and c.t(0) == "0s"
    assert export.Clock(25).t(50) == "2s"
    assert export.parse_time("1001/30000s") * 30 == export.parse_time("1001/1000s")


@needs_ffmpeg
def test_fcpxml_carries_the_whole_edit_and_reads_back(project, tmp_path):
    from .conftest import make_music
    from nullcut import music

    make_music(tmp_path / "lib" / "beat 120.wav", 120.0, 40.0)
    music.import_folder(tmp_path / "lib", log=lambda m: None)
    plan.save_brief(project, {"style": "retention-max", "target_s": None,
                              "captions": {"mode": "word"},
                              "music": {"mode": "library", "mood": "", "energy": "high",
                                        "track_id": None}})
    plan.make_plan(project, log=lambda m: None)
    version, path, meta = pipeline.compile_timeline(project)
    out = export.write_all(project, path, project.path("package"), version)
    checks = out["checks"]["fcpxml"]
    assert checks["ok"], checks
    assert checks["roundtrip"]["durations_match"]
    assert checks["roundtrip"]["broll"] == len(meta["broll"])
    import xml.etree.ElementTree as ET
    root = ET.parse(project.path(out["files"]["fcpxml"])).getroot()
    assert root.get("version") == "1.8"
    spine = root.find("library/event/project/sequence/spine")
    assert len(spine) == len(timeline.load(path)[1])
    assert len(list(spine.iter("keyframe"))) >= 4 * len(meta["zooms"])
    lanes = {e.get("lane") for e in spine.iter("asset-clip") if e.get("lane")}
    assert {"1", "-1"} <= lanes                                   # B-roll and music
    assert len(list(spine.iter("marker"))) == len(meta["beats"])
    assert out["files"]["cube"]                                   # retention-max grades


@needs_ffmpeg
def test_fcpxml_validates_against_apples_dtd_when_available(project):
    dtd = os.environ.get("NULLCUT_FCPXML_DTD")
    lxml = pytest.importorskip("lxml.etree")
    if not dtd or not Path(dtd).exists():
        pytest.skip("set NULLCUT_FCPXML_DTD to FCPXMLv1_8.dtd to run this check")
    plan.make_plan(project, log=lambda m: None)
    version, path, _ = pipeline.compile_timeline(project)
    out = export.write_all(project, path, project.path("package"), version)
    d = lxml.DTD(dtd)
    assert d.validate(lxml.parse(str(project.path(out["files"]["fcpxml"])))), d.error_log
