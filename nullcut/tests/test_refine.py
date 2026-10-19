"""Chat refinement (plan §6 fast path): tweaks, structural patches, undo, the LLM route."""

import json

import pytest

from nullcut import learn, plan, refine
from nullcut.refine import RefineError, apply_patch

from .conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture
def planned(project):
    plan.save_brief(project, {"style": "retention-max", "target_s": None,
                              "captions": {"mode": "word"}})
    plan.make_plan(project, log=lambda m: None)
    return project


def changes(project, text):
    got = refine.interpret(project, text, use_llm=False, log=lambda m: None)
    assert got["kind"] == "tweak", got
    return {c["param"]: c["to"] for c in got["changes"]}


def texts(project, doc=None):
    doc = doc or plan.latest_plan(project, with_beats=True)
    pool = plan.segment_pool(project.clip_index())
    return [pool[s]["text"] for b in doc["beats"] for s in b["segment_ids"]]


def test_tweaks_resolve_to_absolute_values_without_an_llm(planned):
    p = planned
    assert changes(p, "less zoom") == {"zoom.per_minute": 5.0,           # retention-max: 10/min
                                       "pacing.max_static_s": 4.5}
    assert changes(p, "no b-roll") == {"broll.density": 0.0}
    assert changes(p, "music quieter") == {"music.under_speech_db": -24,
                                           "music.gap_db": -14}
    assert changes(p, "warmer") == {"colour_nudge.warmth": 0.04}
    assert changes(p, "black and white") == {"colour_nudge.saturation": 0.0}
    assert changes(p, "make it 2:30") == {"brief.target_s": 150}
    assert changes(p, "keep it under 60 seconds")["brief.target_s"] == pytest.approx(57.1, abs=0.1)
    assert changes(p, "bigger captions") == {"captions.scale": 1.2}
    assert changes(p, "highlight in red") == {"captions.highlight": "#FF3B30"}
    assert changes(p, "remove the music") == {"brief.music.mode": "none"}
    assert changes(p, "cut it to 2:30") == {"brief.target_s": 150}      # not a sentence removal
    assert changes(p, "vertical version too") == {"brief.format": "both"}
    assert changes(p, "switch to the documentary style") == {"brief.style": "documentary"}
    hook = changes(p, "punchier first 5 seconds")
    assert hook["pacing.hook_silence_ms"] < 150 and hook["hook.max_s"] == 5
    assert set(changes(p, "less zoom and no sound effects")) == {
        "zoom.per_minute", "pacing.max_static_s", "sfx.cut_share", "sfx.zoom_share"}
    assert changes(p, "minder zoom en geen muziek") == {"zoom.per_minute": 5.0,
                                                        "pacing.max_static_s": 4.5,
                                                        "brief.music.mode": "none"}


def test_unknown_instruction_without_an_llm_explains_itself(planned):
    got = refine.interpret(planned, "what a lovely afternoon", use_llm=False)
    assert got["kind"] == "unclear" and "less zoom" in got["message"]


def test_tweak_writes_overrides_and_the_compiler_obeys(planned):
    from nullcut import composer, styles

    step = refine.apply(planned, refine.interpret(planned, "no zooms", use_llm=False),
                        "no zooms", log=lambda m: None)
    brief = plan.load_brief(planned)
    assert brief["overrides"]["zoom"]["per_minute"] == 0
    assert styles.for_brief("retention-max", brief)["zoom"]["per_minute"] == 0
    doc = plan.latest_plan(planned, with_beats=True)
    comp = composer.compose(planned.clip_index(), doc, brief, 30.0)
    assert comp.zooms == []
    assert step["kind"] == "tweak" and step["changes"][0]["from"] == 10


def test_structural_removal_is_a_patch_and_undo_restores_it(planned):
    before = texts(planned)
    assert "So This is the plan." in before
    interp = refine.interpret(planned, "drop the part about the plan", use_llm=False)
    assert interp["kind"] == "structural"
    assert all(op["op"] == "remove" for op in interp["patch"])
    step = refine.apply(planned, interp, "drop the part about the plan", log=lambda m: None)
    after = texts(planned)
    assert "So This is the plan." not in after and len(after) == len(before) - 1
    doc = plan.latest_plan(planned, with_beats=True)
    assert len(doc["excluded_ids"]) == 1
    assert step["labelled"] >= 1 and learn.load_examples()[0]["label"] == 0

    plan.make_plan(planned, log=lambda m: None)            # a fresh plan keeps the removal
    assert "So This is the plan." not in texts(planned)

    undo = refine.undo(planned, log=lambda m: None)
    assert undo["kind"] == "undo"
    assert refine.history(planned)[0]["undone"] is True
    assert plan.latest_plan(planned)["restored_from"] == step["plan_before"]
    assert texts(planned) == before


def test_restore_pins_the_sentence_against_the_solver(planned):
    refine.apply(planned, refine.interpret(planned, "drop the part about the plan",
                                           use_llm=False), "x", log=lambda m: None)
    interp = refine.interpret(planned, "bring back the part about the plan", use_llm=False)
    assert interp["kind"] == "structural"
    refine.apply(planned, interp, "bring back", log=lambda m: None)
    doc = plan.latest_plan(planned, with_beats=True)
    assert "So This is the plan." in texts(planned, doc)
    pool = plan.segment_pool(planned.clip_index())
    sid = next(s for s in pool if pool[s]["text"] == "So This is the plan.")
    assert sid in doc["pinned_ids"] and sid not in doc["excluded_ids"]


def test_open_on_moves_the_old_hook_back_into_the_story(planned):
    before = texts(planned)
    assert before[0] == "And it ends well."          # the heuristic's hook
    with pytest.raises(RefineError, match="already opens"):
        refine.interpret(planned, "open on the part that ends well instead", use_llm=False)
    interp = refine.interpret(planned, "open on the line where the second clip starts",
                              use_llm=False)
    refine.apply(planned, interp, "open on", log=lambda m: None)
    doc = plan.latest_plan(planned, with_beats=True)
    after = texts(planned, doc)
    assert doc["beats"][0]["role"] == "hook" and after[0] == "Second clip starts here."
    assert sorted(after) == sorted(before)           # same sentences, nothing duplicated
    # The old hook went back to its place in the story: after the clip-two opener.
    assert after.index("And it ends well.") > after.index("Second clip starts here.")


def test_locked_beats_refuse_changes_and_ambiguity_asks(planned):
    doc = plan.latest_plan(planned, with_beats=True)
    pool = plan.segment_pool(planned.clip_index())
    target = next(b for b in doc["beats"]
                  if any(pool[s]["text"] == "So This is the plan." for s in b["segment_ids"]))
    edits = [{"id": b["id"], "locked": b["id"] == target["id"]} for b in doc["beats"]]
    plan.edit_plan(planned, edits, log=lambda m: None)
    with pytest.raises(RefineError, match="locked"):
        refine.interpret(planned, "drop the part about the plan", use_llm=False)
    with pytest.raises(RefineError, match="no kept sentence"):
        refine.interpret(planned, "drop the bit where I cough", use_llm=False)


def test_json_patch_follows_rfc_6902():
    doc = {"beats": [{"id": "a", "segment_ids": [1, 2, 3]}, {"id": "b", "segment_ids": []}]}
    out = apply_patch(doc, [
        {"op": "test", "path": "/beats/0/id", "value": "a"},
        {"op": "move", "from": "/beats/0/segment_ids/2", "path": "/beats/1/segment_ids/0"},
        {"op": "remove", "path": "/beats/0/segment_ids/0"},
        {"op": "add", "path": "/beats/1/segment_ids/-", "value": 9},
        {"op": "replace", "path": "/beats/0/id", "value": "z"},
        {"op": "move", "from": "/beats/1", "path": "/beats/0"},
    ])
    assert out == {"beats": [{"id": "b", "segment_ids": [3, 9]}, {"id": "z", "segment_ids": [2]}]}
    assert doc["beats"][0]["segment_ids"] == [1, 2, 3]          # input untouched
    with pytest.raises(RefineError):
        apply_patch(doc, [{"op": "remove", "path": "/beats/5"}])
    with pytest.raises(RefineError):
        apply_patch(doc, [{"op": "test", "path": "/beats/0/id", "value": "q"}])


def test_llm_route_validates_and_repairs_its_patch(planned, fake_llm):
    aliases = plan.Aliases(planned.clip_index())
    doc = plan.latest_plan(planned, with_beats=True)
    first = aliases.short(doc["beats"][0]["segment_ids"][0])
    fake = fake_llm([
        {"content": json.dumps({"kind": "structural", "summary": "bad", "tweaks": [],
                                "question": "", "patch": [
                                    {"op": "remove", "path": "/beats/9/segment_ids/0"}]})},
        {"content": json.dumps({"kind": "structural", "summary": "drop the opener",
                                "tweaks": [], "question": "", "patch": [
                                    {"op": "test", "path": "/beats/0/segment_ids/0",
                                     "value": first},
                                    {"op": "remove", "path": "/beats/0/segment_ids/0"}]})},
    ])
    got = refine.interpret(planned, "the opening line feels slow, lose it", log=lambda m: None)
    assert got["kind"] == "structural" and got["by"] == "llm:fake"
    assert got["patch"][0]["value"] == doc["beats"][0]["segment_ids"][0]   # unaliased
    assert fake.requests[0]["response_format"]["type"] == "json_schema"
    assert "cannot be applied" in fake.requests[1]["messages"][-1]["content"]


def test_llm_tweak_params_are_bounded(planned, fake_llm):
    fake_llm([{"content": json.dumps({"kind": "tweak", "summary": "more zoom",
                                      "tweaks": [{"param": "zoom.per_minute", "op": "scale",
                                                  "value": 100}],
                                      "patch": [], "question": ""})}])
    got = refine.interpret(planned, "can you make it feel more dynamic", log=lambda m: None)
    assert got["changes"][0]["to"] == 30          # clamped to the parameter's range
