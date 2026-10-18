"""Planning: deterministic planner, validation, locks, questions, and the LLM protocol."""

import json

import pytest

from nullcut import plan, solver

from .conftest import make_clip_record, needs_ffmpeg

pytestmark = needs_ffmpeg


def kept(doc):
    return [s for b in doc["beats"] for s in b["segment_ids"]]


def text_of(project, sid):
    return plan.segment_pool(project.clip_index())[sid]["text"]


def test_heuristic_plan_opens_on_the_line_the_brief_names(project):
    plan.save_brief(project, {"text": "open on the plan", "style": "explainer"})
    doc = plan.make_plan(project, log=lambda m: None)
    assert doc["planner"] == "heuristic"
    assert doc["beats"][0]["role"] == "hook"
    assert "plan" in text_of(project, doc["beats"][0]["segment_ids"][0]).lower()
    assert len(set(kept(doc))) == len(kept(doc))  # no sentence used twice
    assert {b["role"] for b in doc["beats"]} <= set(plan.ROLES)


def test_brief_saves_merge_instead_of_replacing(project):
    plan.save_brief(project, {"text": "keep me", "style": "retention-max"})
    plan.save_brief(project, {"target_s": 20})
    brief = plan.load_brief(project)
    assert (brief["text"], brief["style"], brief["target_s"]) == ("keep me", "retention-max", 20)
    with pytest.raises(ValueError):
        plan.save_brief(project, {"target_s": 5})


def test_filler_only_sentences_never_reach_a_planner():
    rec = make_clip_record("c_f", [("Hello", 0.2, 0.5), ("there.", 0.55, 0.9),
                                   ("Um.", 2.0, 2.3), ("Bye.", 3.5, 3.9)], 5.0)
    index = {"clips": [rec]}
    from nullcut import styles
    lengths = plan.segment_lengths(index, styles.get("explainer"))["body"]
    pool = plan.segment_pool(index, lengths)
    assert [p["text"] for p in pool.values()] == ["Hello there.", "Bye."]


def test_validation_catches_unknown_reused_and_broken_locks(project):
    pool = plan.segment_pool(project.clip_index())
    a, b = list(pool)[:2]
    beats = [{"id": "b1", "role": "hook", "title": "x", "segment_ids": [a, "c_nope:s000"]},
             {"id": "b2", "role": "chorus", "title": "y", "segment_ids": [a]}]
    locked = [{"id": "b9", "title": "z", "segment_ids": [b]}]
    errors = " | ".join(plan.validate(beats, pool, locked))
    assert "unknown segment c_nope:s000" in errors
    assert "used twice" in errors
    assert "role must be one of" in errors
    assert "locked beat 'z' changed or missing" in errors


def test_locks_survive_a_replan_and_a_question_round(project):
    plan.save_brief(project, {"style": "explainer", "target_s": None})
    doc = plan.make_plan(project, log=lambda m: None)
    hook = doc["beats"][0]
    plan.edit_plan(project, [{"id": b["id"], "locked": b["id"] == hook["id"]}
                             for b in doc["beats"]], log=lambda m: None)
    plan.save_brief(project, {"target_s": 600})       # impossible: triggers the question
    asked = plan.make_plan(project, log=lambda m: None)
    assert asked["questions"] and not asked["beats"]
    q = asked["questions"][0]["text"]
    again = plan.make_plan(project, log=lambda m: None)
    assert again["questions"][0]["text"] == q          # unanswered: asked again, budget unspent
    assert json.loads(project.path("plans", "questions.json").read_text()) == [q]
    done = plan.make_plan(project, answers={q: "ship it"}, log=lambda m: None)
    assert done["beats"][0]["locked"] and done["beats"][0]["segment_ids"] == hook["segment_ids"]
    assert plan.load_brief(project)["target_s"] is None  # the answer updated the brief


def test_storyboard_edit_reorders_deletes_and_removes(project):
    doc = plan.make_plan(project, log=lambda m: None)
    first, *rest = doc["beats"]
    edits = [{"id": b["id"], "locked": False, "segment_ids": b["segment_ids"]} for b in rest]
    edits.append({"id": first["id"], "locked": True, "segment_ids": first["segment_ids"]})
    edits = edits[1:]  # delete the second beat
    out = plan.edit_plan(project, edits, log=lambda m: None)
    assert [b["id"] for b in out["beats"]] == [e["id"] for e in edits]
    assert out["beats"][-1]["locked"] and out["version"] == doc["version"] + 1
    with pytest.raises(ValueError):
        plan.edit_plan(project, [{"id": "nope"}], log=lambda m: None)


def test_beat_solver_caps_the_hook_and_keeps_every_beat_alive():
    beats = [solver.BeatIn("b1", "hook", ["h1", "h2"], 4.0),
             solver.BeatIn("b2", "body", ["x1", "x2", "x3"], 20.0),
             solver.BeatIn("b3", "cta", ["c1"], 2.0)]
    lengths = {"h1": 5.0, "h2": 3.0, "x1": 8.0, "x2": 8.0, "x3": 8.0, "c1": 6.0}
    scores = {"h1": 0.9, "h2": 0.5, "x1": 0.2, "x2": 0.8, "x3": 0.7, "c1": 0.1}
    sol = solver.solve(beats, lengths, scores, target_s=25.0, hook_max_s=4.0)
    assert sol.kept["b1"] == ["h2"]       # h1 is better but breaks the 4 s hook cap
    assert sol.kept["b3"] == ["c1"]       # lowest score, but a beat may not vanish
    assert sol.status == "met"
    locked = [solver.BeatIn("b1", "hook", ["h1"], None, locked=True)]
    assert solver.solve(locked, lengths, scores, 3.0, hook_max_s=4.0).kept["b1"] == ["h1"]


# --- LLM planner protocol ----------------------------------------------------------------

def _short_ids(project):
    aliases = plan.Aliases(project.clip_index())
    return aliases, [aliases.short(s) for s in plan.segment_pool(project.clip_index())]


def _plan_json(ids, roles=("hook", "body", "cta")):
    beats = [{"role": r, "title": f"{r} beat", "intent": "test", "segments": [i],
              "emphasis": [], "music_energy": "mid"} for r, i in zip(roles, ids)]
    return {"content": json.dumps({"summary": "scripted", "beats": beats})}


def test_llm_planner_tools_schema_and_repair(project, fake_llm):
    _, ids = _short_ids(project)
    fake = fake_llm([
        {"content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {
            "name": "search_clips", "arguments": json.dumps({"query": "plan"})}}]},
        {"content": "I have what I need."},
        _plan_json([ids[0], "c9:s999", ids[1]]),          # invalid: unknown id
        _plan_json([ids[2], ids[0], ids[1]]),             # repaired
    ])
    doc = plan.make_plan(project, log=lambda m: None)
    assert doc["planner"] == "llm:fake"
    assert len(fake.requests) == 4
    assert fake.requests[0]["tools"][0]["function"]["name"] == "search_clips"
    tool_reply = next(m for m in fake.requests[1]["messages"] if m["role"] == "tool")
    assert "plan" in tool_reply["content"].lower()
    assert fake.requests[2]["response_format"]["type"] == "json_schema"
    assert "unknown segment" in fake.requests[3]["messages"][-1]["content"]
    pool = list(plan.segment_pool(project.clip_index()))
    assert doc["beats"][0]["segment_ids"] == [pool[2]]   # short ids mapped back


def test_llm_question_is_recorded_and_capped(project, fake_llm):
    _, ids = _short_ids(project)
    fake = fake_llm([
        {"content": "", "tool_calls": [{"id": "q", "type": "function", "function": {
            "name": "ask_user", "arguments": json.dumps({"question": "Which clip opens?"})}}]},
        {"content": "done"},
        _plan_json(ids[:3]),
    ])
    doc = plan.make_plan(project, log=lambda m: None)
    assert [q["text"] for q in doc["questions"]] == ["Which clip opens?"]
    assert "question recorded" in fake.requests[1]["messages"][-1]["content"]
    tools = plan.PlannerTools(project.clip_index(), plan.Aliases(project.clip_index()), 0)
    assert tools.call("ask_user", {"question": "again?"}).startswith("error")


def test_unreachable_llm_falls_back_to_the_deterministic_planner(project, monkeypatch):
    monkeypatch.delenv("NULLCUT_LLM", raising=False)
    monkeypatch.setenv("NULLCUT_LLM_URL", "http://127.0.0.1:9/v1")
    assert plan.make_plan(project, log=lambda m: None)["planner"] == "heuristic"


def test_llm_that_never_produces_a_valid_plan_falls_back(project, fake_llm):
    fake_llm([{"content": "done"}, {"content": "not json"}, {"content": "{\"beats\": []}"}])
    assert plan.make_plan(project, log=lambda m: None)["planner"] == "heuristic"


def test_contact_sheet_shrinks_to_budget_but_keeps_every_id():
    words, t = [], 0.0
    for n in range(40):
        for k in range(20):
            words.append((f"word{k}" + ("." if k == 19 else ""), t, t + 0.2))
            t += 0.25
        t += 1.5
    index = {"clips": [make_clip_record("c_long", words, t + 1)]}
    aliases = plan.Aliases(index)
    ids = [aliases.short(s) for s in plan.segment_pool(index)]
    full = plan.contact_sheet(index, aliases)
    tight = plan.contact_sheet(index, aliases, budget_tokens=len(full) // 8)
    assert len(tight) < len(full) // 2
    assert all(i in tight for i in ids)
    assert "…" in tight
