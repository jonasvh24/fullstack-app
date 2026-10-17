import argparse

import pytest

from nullcut import analyse
from nullcut.cli import parse_duration
from nullcut.compiler import CutOptions, compile_rough, contiguous

from .conftest import CLIP_A, CLIP_B, make_clip_record


def index_of(*clips):
    idx = {"version": 1, "clips": list(clips)}
    analyse.score_segments(idx["clips"])
    return idx


def kept_words(items):
    return [w["w"] for it in items for w in it.words]


@pytest.mark.parametrize("text,seconds", [
    ("8m", 480), ("90s", 90), ("90", 90), ("1h2m", 3720), ("4:00", 240),
    ("1:02:03", 3723), ("2m30s", 150), ("0.5m", 30),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds)


def test_parse_duration_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration("eight minutes")


def test_hard_and_isolated_soft_fillers_are_removed():
    res = compile_rough(index_of(make_clip_record("c_a", CLIP_A, 8.0)), CutOptions())
    words = kept_words(res.items)
    assert "um" not in words
    assert "So" not in words  # isolated by pauses on both sides
    assert words == ["Welcome", "to", "the", "channel.", "Today", "we", "build", "things.",
                     "This", "is", "the", "plan."]
    assert res.report["fillers"] == 2
    assert any("filler 'um'" in r for it in res.items for r in it.rules)


def test_soft_filler_inside_a_sentence_survives():
    script = [("I", 0.5, 0.6), ("like", 0.65, 0.9), ("this.", 0.95, 1.4)]
    res = compile_rough(index_of(make_clip_record("c_x", script, 2.0)), CutOptions())
    assert kept_words(res.items) == ["I", "like", "this."]


def test_dutch_dus_mid_sentence_is_kept_but_er_is_never_a_filler():
    script = [("Er", 0.5, 0.7), ("is", 0.75, 0.9), ("dus", 0.95, 1.1), ("tijd.", 1.15, 1.6)]
    res = compile_rough(index_of(make_clip_record("c_nl", script, 2.0, lang="nl")), CutOptions())
    assert kept_words(res.items) == ["Er", "is", "dus", "tijd."]


def test_keep_fillers_flag():
    res = compile_rough(index_of(make_clip_record("c_a", CLIP_A, 8.0)),
                        CutOptions(remove_fillers=False))
    assert "um" in kept_words(res.items) and "So" in kept_words(res.items)


def test_handles_never_reach_into_a_removed_filler():
    res = compile_rough(index_of(make_clip_record("c_a", CLIP_A, 8.0)), CutOptions())
    um = next(w for w in CLIP_A if w[0] == "um")
    for it in res.items:
        assert it.src_out <= um[1] + 1 / 30 or it.src_in >= um[2] - 1 / 30


def test_short_pauses_stay_and_long_pauses_cut():
    script = [("a", 0.0, 0.3), ("b", 0.5, 0.8), ("c", 2.0, 2.3)]  # 0.2 s gap, then 1.2 s
    rec = make_clip_record("c_p", script, 3.0)
    rec["segments"] = [{"id": "c_p:s000", "start": 0.0, "end": 2.3, "words": [0, 3],
                        "text": "a b c", "features": {"speech_rate": 1, "confidence": 1,
                                                      "energy": -20}}]
    res = compile_rough(index_of(rec), CutOptions())
    assert [kept_words([it]) for it in res.items] == [["a", "b"], ["c"]]
    assert any(r.startswith("jump cut") for r in res.items[1].rules)


def test_edges_sit_on_the_frame_grid_and_sync_holds():
    fps = 30.0
    res = compile_rough(index_of(make_clip_record("c_a", CLIP_A, 8.0),
                                 make_clip_record("c_b", CLIP_B, 5.5)), CutOptions(fps=fps))
    for it in res.items:
        assert abs(it.src_in * fps - round(it.src_in * fps)) < 1e-6
        assert it.frames == round((it.src_out - it.src_in) * fps)
    assert res.report["kept_s"] == pytest.approx(sum(it.frames for it in res.items) / fps,
                                                 abs=0.01)


def test_contiguous_spans_survive_quantisation():
    # Two sentences with a 0.1 s gap: handles overlap, so they must meet exactly.
    script = [("One.", 0.5, 1.0), ("Two.", 1.1, 1.6)]
    res = compile_rough(index_of(make_clip_record("c_c", script, 2.0)), CutOptions(fps=25))
    assert len(res.items) == 2
    assert contiguous(res.items[0], res.items[1], 25)
    assert res.report["jump_cuts"] == 0


def test_target_tightens_silence_before_dropping_material():
    # One sentence with 0.32 s pauses: kept at the 0.35 s default, cut at 0.30 s.
    script, t = [], 0.5
    for n in range(10):
        script.append((f"w{n}" + ("." if n == 9 else ""), t, t + 0.3))
        t += 0.62
    rec = make_clip_record("c_t", script, t + 1.0)
    tight = compile_rough(index_of(rec), CutOptions(silence_s=0.30)).report["kept_s"]
    loose = compile_rough(index_of(rec), CutOptions()).report["kept_s"]
    assert tight < loose * 0.9

    res = compile_rough(index_of(rec), CutOptions(target_s=tight))
    assert res.report["tightened_to"] == [0.30]
    assert not res.report["dropped_segments"]
    assert res.report["target_met"]


def test_target_drops_lowest_scoring_sentences_and_respects_window():
    a = make_clip_record("c_a", CLIP_A, 8.0)
    b = make_clip_record("c_b", CLIP_B, 5.5)
    idx = index_of(a, b)
    lowest = min((s for c in idx["clips"] for s in c["segments"]), key=lambda s: s["score"])
    full = compile_rough(idx, CutOptions()).report["kept_s"]
    target = full - 1.5
    res = compile_rough(idx, CutOptions(target_s=target))
    assert res.report["dropped_segments"]
    assert res.report["dropped_segments"][0]["segment"] == lowest["id"]
    assert res.report["target_met"], res.report
    # Story order survives the fit.
    order = [(it.clip_id, it.src_in) for it in res.items]
    assert order == sorted(order)


def test_short_footage_reports_shortfall_and_never_pads():
    res = compile_rough(index_of(make_clip_record("c_a", CLIP_A, 8.0)), CutOptions(target_s=60))
    assert res.report["target_met"] is False
    assert res.report["shortfall_s"] > 50
    assert res.report["kept_s"] < 10


def test_clip_without_speech_is_skipped_and_reported():
    silent = make_clip_record("c_s", [], 4.0)
    res = compile_rough(index_of(make_clip_record("c_a", CLIP_A, 8.0), silent), CutOptions())
    assert res.report["skipped_no_speech"] == ["c_s.mp4"]
    assert {it.clip_id for it in res.items} == {"c_a"}
