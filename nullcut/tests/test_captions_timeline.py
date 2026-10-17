from pathlib import Path

import pytest

from nullcut import analyse, asr, captions, timeline
from nullcut.compiler import CutOptions, Item, compile_rough
from nullcut.project import Project, SUBDIRS

from .conftest import CLIP_A, make_clip_record


def test_output_words_follow_the_cut():
    items = [Item("c", "s0", 1.0, 2.0, 30, [{"w": "a", "s": 1.2, "e": 1.5}]),
             Item("c", "s1", 5.0, 6.0, 30, [{"w": "b", "s": 5.1, "e": 5.4}])]
    out = captions.output_words(items, 30.0)
    assert out[0]["s"] == pytest.approx(0.2) and out[1]["s"] == pytest.approx(1.1)


def test_grouping_breaks_on_sentence_word_count_and_gap():
    words = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, w in
             enumerate("one two three four five six seven eight.".split())]
    words.append({"w": "after", "s": 5.0, "e": 5.3})
    blocks = captions.group(words, "clean")
    assert [len(b) for b in blocks] == [6, 2, 1]
    assert [len(b) for b in captions.group(words, "word")] == [3, 3, 2, 1]


def test_escape_neutralises_ass_syntax():
    assert captions.escape(r"a{\b1}b\N") == "a(/b1)b/N"


def test_word_style_highlights_each_word_once():
    words = [{"w": "go", "s": 0.0, "e": 0.3}, {"w": "now", "s": 0.35, "e": 0.7}]
    events = captions.build_events(words, "word")
    assert len(events) == 2
    assert events[0].text.startswith("{\\c") and "NOW" in events[0].text
    assert events[1].text.startswith("GO ")


def test_slice_shifts_and_clips():
    ev = [captions.Event(0.5, 2.5, "x")]
    out = captions.slice_events(ev, 1.0, 2.0)
    assert (out[0].start, out[0].end) == (0.0, 1.0)
    assert captions.slice_events(ev, 3.0, 4.0) == []


def test_ass_and_srt_formats():
    ass = captions.ass_document([captions.Event(61.234, 62.0, "hi")], 1920, 1080, "clean")
    assert "PlayResY: 1080" in ass and "0:01:01.23,0:01:02.00" in ass
    srt = captions.srt([{"w": "hi.", "s": 3661.5, "e": 3662.0}])
    # 0.5 s word, held to the 0.6 s minimum read time.
    assert srt.startswith("1\n01:01:01,500 --> 01:01:02,100\nhi.")


def test_sidecar_formats_and_missing_times(tmp_path: Path):
    p = tmp_path / "x.words.json"
    p.write_text('{"segments": [{"words": [{"word": "a", "start": 0.0, "end": 0.2, "score": 0.9},'
                 '{"word": "42"}, {"word": "b", "start": 1.0, "end": 1.2}]}]}')
    words, _ = asr.load_sidecar(p)
    assert [w["w"] for w in words] == ["a", "42", "b"]
    assert 0.2 <= words[1]["s"] < words[1]["e"] <= 1.0


def test_timeline_round_trip(tmp_path: Path):
    project = Project(tmp_path / "p")
    for sub in SUBDIRS:
        project.path(sub).mkdir(parents=True)
    idx = {"version": 1, "clips": [make_clip_record("c_a", CLIP_A, 8.0)]}
    analyse.score_segments(idx["clips"])
    res = compile_rough(idx, CutOptions())
    meta = {"fps": 30.0, "captions": {"enabled": True, "style": "clean"}, "report": res.report,
            "input_hash": "abc"}
    v, path, created = timeline.save(project, res.items, idx, meta)
    assert (v, created) == (1, True)
    assert timeline.save(project, res.items, idx, meta)[:3] == (1, path, False)
    loaded_meta, items = timeline.load(path)
    assert loaded_meta["captions"]["enabled"] is True
    assert [(i.src_in, i.frames, i.words) for i in items] == \
           [(i.src_in, i.frames, i.words) for i in res.items]
    v2, _, _ = timeline.save(project, res.items, idx, {**meta, "input_hash": "def"})
    assert v2 == 2
