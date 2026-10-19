"""Edit planning: brief in, Edit Plan out (plan §2, §6).

The Edit Plan is semantic: beats, intent, which sentences serve each beat,
which words to punch. It never mentions frames. The LLM planner reads a
contact sheet (a document about the footage, never the footage) and may
call four tools. A deterministic planner produces the same shape with no
model at all: the offline fallback and the baseline any LLM must beat.

Every plan is validated before it is saved: unknown segment ids, reused
segments and broken locks are errors, whoever wrote the plan.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Callable

from . import compiler, config, llm, solver, styles
from .project import Project, read_json, stable_hash, write_json

ROLES = styles.ROLES
ENERGY = ("low", "mid", "high")
MAX_QUESTIONS = 3
MAX_TOOL_ROUNDS = 8
CONTACT_SHEET_TOKENS = 32_000  # plan §4: two hours of footage fits in 32k

Log = Callable[[str], None]

BRIEF_DEFAULT = {
    "text": "",
    "target_s": None,
    "tolerance": config.TARGET_TOLERANCE,
    "format": "16:9",
    "style": "explainer",
    "music": {"mode": "none", "mood": "", "energy": "mid", "track_id": None},
    "captions": {"mode": "clean"},
    "extras": {"zoom": 1.0, "broll": 1.0, "hook_required": True},
    "thumbnail_profile": "",
    "title_refs": "",
    "overrides": {},   # refinement tweaks onto the style, absolute values (plan §6)
}
MUSIC_MODES = ("none", "library", "track", "generate")

# Words that carry a claim or a jolt; the deterministic planner punches these.
EMPHASIS_LEXICON = {
    "never", "always", "best", "worst", "biggest", "smallest", "fastest", "first", "last",
    "only", "every", "free", "secret", "insane", "huge", "massive", "impossible", "simple",
    "whole", "entirely", "nooit", "altijd", "beste", "grootste", "enige", "gratis", "geheim",
}
CTA_WORDS = {"subscribe", "watching", "comment", "follow", "link", "abonneer", "bedankt",
             "kijken", "volg"}
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "that", "this",
        "we", "i", "you", "for", "with", "be", "are", "was", "at", "as", "de", "het", "een",
        "en", "van", "dat", "ik", "je", "op", "met", "voor"}


# --- brief ------------------------------------------------------------------

def load_brief(project: Project) -> dict:
    path = project.path("plans", "brief.json")
    brief = json.loads(json.dumps(BRIEF_DEFAULT))
    if path.exists():
        saved = read_json(path)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(brief.get(k), dict):
                brief[k].update(v)
            else:
                brief[k] = v
    return brief


def save_brief(project: Project, brief: dict) -> dict:
    """Merge onto the saved brief, so a partial update never wipes the rest."""
    merged = load_brief(project)
    for k, v in brief.items():
        if k not in merged:
            continue
        if k == "overrides":
            # Replaced whole: a refinement or an undo sets the complete set.
            if not isinstance(v, dict):
                raise ValueError("overrides must be an object")
            merged[k] = v
        elif isinstance(v, dict) and isinstance(merged[k], dict):
            merged[k].update(v)
        else:
            merged[k] = v
    if merged["target_s"] is not None:
        t = float(merged["target_s"])
        if not 15 <= t <= 1800:
            raise ValueError("length must be between 0:15 and 30:00")
        merged["target_s"] = t
    if merged["format"] not in ("16:9", "9:16", "both"):
        raise ValueError("format must be 16:9, 9:16 or both")
    styles.get(merged["style"])  # raises on unknown style
    if merged["captions"]["mode"] not in ("off", "clean", "word"):
        raise ValueError("captions must be off, clean or word")
    if merged["music"]["mode"] not in MUSIC_MODES:
        raise ValueError(f"music must be one of {', '.join(MUSIC_MODES)}")
    write_json(project.path("plans", "brief.json"), merged)
    return merged


# --- footage views ------------------------------------------------------------

def segment_pool(index: dict, usable: dict[str, float] | None = None) -> dict[str, dict]:
    """Sentence units in story order, with the fields a planner may see.

    With `usable`, sentences that jump cuts reduce to nothing (a lone "um")
    are left out: no planner should spend a beat on them.
    """
    pool = {}
    for c in index["clips"]:
        for s in c["segments"]:
            if usable is not None and usable.get(s["id"], 0.0) <= 0.0:
                continue
            pool[s["id"]] = {"id": s["id"], "clip_id": c["id"], "start": s["start"],
                             "end": s["end"], "text": s["text"], "score": s.get("score", 0.5),
                             "keyframe": s.get("keyframe"), "caption": _caption_at(c, s)}
    return pool


def _caption_at(clip: dict, seg: dict) -> str:
    mid = (seg["start"] + seg["end"]) / 2
    for shot in clip.get("shots", []):
        if shot["start"] <= mid < shot["end"]:
            return shot.get("caption", "")
    return ""


def segment_lengths(index: dict, style: dict) -> dict[str, dict[str, float]]:
    """Seconds each sentence runs after jump cuts, at the body and the hook thresholds."""
    out = {}
    for key, ms in (("body", style["pacing"]["silence_ms"]),
                    ("hook", style["pacing"]["hook_silence_ms"])):
        items, _ = compiler.build([c for c in index["clips"] if c["words"]], ms / 1000, True)
        lengths: dict[str, float] = {}
        for it in items:
            lengths[it.segment_id] = lengths.get(it.segment_id, 0.0) + it.duration
        out[key] = lengths
    return out


class Aliases:
    """Short ids for the LLM ("c2:s014"), mapped back to content ids on the way out."""

    def __init__(self, index: dict):
        self.to_short = {c["id"]: f"c{n}" for n, c in enumerate(index["clips"], 1)}
        self.to_long = {v: k for k, v in self.to_short.items()}

    def short(self, seg_id: str) -> str:
        clip, rest = seg_id.split(":", 1)
        return f"{self.to_short.get(clip, clip)}:{rest}"

    def long(self, seg_id: str) -> str:
        clip, _, rest = seg_id.partition(":")
        return f"{self.to_long.get(clip, clip)}:{rest}"


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def contact_sheet(index: dict, aliases: Aliases, budget_tokens: int = CONTACT_SHEET_TOKENS,
                  usable: dict[str, float] | None = None) -> str:
    """The footage as the planner sees it: one line per sentence and per B-roll shot.

    Over budget, transcript excerpts shrink first (the planner can fetch any
    sentence in full with get_segment), so every id stays visible.
    """
    def render(max_words: int | None) -> str:
        lines = []
        for c in index["clips"]:
            head = (f"## {aliases.to_short[c['id']]} {c['name']} ({_mmss(c['duration'])}, "
                    f"{'speech' if c['words'] else 'B-roll, no speech'})")
            lines.append(head)
            for s in c["segments"]:
                if usable is not None and usable.get(s["id"], 0.0) <= 0.0:
                    continue
                words = s["text"].split()
                text = " ".join(words[:max_words]) + (" …" if max_words and len(words) > max_words
                                                      else "")
                cap = _caption_at(c, s)
                lines.append(f"{aliases.short(s['id'])} [{_mmss(s['start'])}-{_mmss(s['end'])}] "
                             f"score {s.get('score', 0.5):.2f} \"{text}\""
                             + (f" | scene: {cap}" if cap else ""))
            if not c["words"]:
                for sh in c.get("shots", []):
                    lines.append(f"{aliases.short(sh['id'])} [{_mmss(sh['start'])}-"
                                 f"{_mmss(sh['end'])}] B-roll score {sh.get('score', 0.5):.2f}"
                                 + (f" | scene: {sh['caption']}" if sh.get("caption") else ""))
        return "\n".join(lines)

    for max_words in (None, 24, 12, 6):
        sheet = render(max_words)
        if len(sheet) / 4 <= budget_tokens:
            return sheet
    return sheet


# --- tools the LLM may call --------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if t not in STOP]


class PlannerTools:
    def __init__(self, index: dict, aliases: Aliases, questions_left: int):
        self.index = index
        self.aliases = aliases
        self.pool = segment_pool(index)
        self.questions_left = questions_left
        self.asked: list[str] = []
        docs = {sid: _tokens(s["text"] + " " + s["caption"]) for sid, s in self.pool.items()}
        for c in index["clips"]:
            if not c["words"]:
                for sh in c.get("shots", []):
                    docs[sh["id"]] = _tokens(sh.get("caption", "") + " " + c["name"])
        self.docs = docs
        df = Counter(t for d in docs.values() for t in set(d))
        self.idf = {t: math.log(1 + len(docs) / n) for t, n in df.items()}

    SPECS = [
        {"type": "function", "function": {
            "name": "search_clips",
            "description": "Search transcripts and scene descriptions. Returns the best-matching "
                           "segment and B-roll ids with their text.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "get_segment",
            "description": "Full transcript, timing and scene of one segment, with its neighbours.",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                           "required": ["id"]}}},
        {"type": "function", "function": {
            "name": "get_transcript",
            "description": "Transcript of a clip between two times in seconds.",
            "parameters": {"type": "object", "properties": {
                "clip": {"type": "string"}, "start": {"type": "number"}, "end": {"type": "number"}},
                "required": ["clip", "start", "end"]}}},
        {"type": "function", "function": {
            "name": "ask_user",
            "description": "Ask the creator one question. Only when the brief and the footage "
                           "disagree. Planning pauses until they answer.",
            "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                           "required": ["question"]}}},
    ]

    def call(self, name: str, args: dict) -> str:
        try:
            return getattr(self, f"_{name}")(**args)
        except (AttributeError, TypeError) as exc:
            return f"error: {exc}"

    def _search_clips(self, query: str) -> str:
        q = _tokens(query)
        scored = []
        for sid, doc in self.docs.items():
            tf = Counter(doc)
            score = sum(self.idf.get(t, 0) * tf[t] / (1 + len(doc) / 20) for t in q)
            if score > 0:
                scored.append((score, sid))
        scored.sort(reverse=True)
        lines = []
        for _, sid in scored[:8]:
            seg = self.pool.get(sid)
            text = seg["text"] if seg else " ".join(self.docs[sid])
            lines.append(f"{self.aliases.short(sid)}: {text}")
        return "\n".join(lines) or "no matches"

    def _get_segment(self, id: str) -> str:
        sid = self.aliases.long(id)
        if sid not in self.pool:
            return f"error: no segment {id}"
        ids = list(self.pool)
        i = ids.index(sid)
        seg = self.pool[sid]
        out = {"id": id, "start": seg["start"], "end": seg["end"], "text": seg["text"],
               "scene": seg["caption"], "score": seg["score"],
               "previous": self.pool[ids[i - 1]]["text"] if i > 0 else None,
               "next": self.pool[ids[i + 1]]["text"] if i + 1 < len(ids) else None}
        return json.dumps(out, ensure_ascii=False)

    def _get_transcript(self, clip: str, start: float, end: float) -> str:
        cid = self.aliases.to_long.get(clip, clip)
        for c in self.index["clips"]:
            if c["id"] == cid:
                return " ".join(w["w"] for w in c["words"] if start <= w["s"] < end) or "(silence)"
        return f"error: no clip {clip}"

    def _ask_user(self, question: str) -> str:
        if self.questions_left <= 0:
            return "error: question budget spent; decide yourself and note the assumption"
        self.asked.append(question.strip())
        self.questions_left -= 1
        return "question recorded; finish with your best plan assuming the likeliest answer"


# --- validation ------------------------------------------------------------------

def validate(beats: list[dict], pool: dict[str, dict], locked: list[dict]) -> list[str]:
    errors = []
    seen: dict[str, str] = {}
    for b in beats:
        if b.get("role") not in ROLES:
            errors.append(f"beat {b.get('title')!r}: role must be one of {', '.join(ROLES)}")
        for sid in b.get("segment_ids", []):
            if sid not in pool:
                errors.append(f"beat {b.get('title')!r}: unknown segment {sid}")
            elif sid in seen:
                errors.append(f"segment {sid} used twice ({seen[sid]!r} and {b.get('title')!r})")
            else:
                seen[sid] = b.get("title", "")
    by_id = {b["id"]: b for b in beats}
    for lb in locked:
        got = by_id.get(lb["id"])
        if got is None or got["segment_ids"] != lb["segment_ids"]:
            errors.append(f"locked beat {lb['title']!r} changed or missing")
    if not any(b.get("segment_ids") for b in beats):
        errors.append("plan has no segments")
    return errors


def _merge_locked(beats: list[dict], locked: list[dict], previous: list[dict]) -> list[dict]:
    """Locked beats survive any re-plan unchanged and at their old position."""
    locked_ids = {s for lb in locked for s in lb["segment_ids"]}
    out = [dict(b, segment_ids=[s for s in b["segment_ids"] if s not in locked_ids])
           for b in beats if b["id"] not in {lb["id"] for lb in locked}]
    out = [b for b in out if b["segment_ids"]]
    order = [b["id"] for b in previous]
    for lb in sorted(locked, key=lambda b: order.index(b["id"]) if b["id"] in order else 0):
        pos = order.index(lb["id"]) if lb["id"] in order else len(out)
        out.insert(min(pos, len(out)), lb)
    return out


# --- planners ----------------------------------------------------------------------

def _emphasis(text: str) -> list[str]:
    words = [w.strip(".,!?;:\"'").lower() for w in text.split()]
    picks = [w for w in words if w in EMPHASIS_LEXICON or any(ch.isdigit() for ch in w)]
    if not picks:
        long_words = sorted({w for w in words if len(w) >= 8 and w not in STOP}, key=len,
                            reverse=True)
        picks = long_words[:1]
    return picks[:3]


def plan_heuristic(brief: dict, style: dict, index: dict, lengths: dict[str, float]) -> dict:
    """Deterministic planner: story order, best matching sentence as the hook, style shares."""
    pool = segment_pool(index, lengths)
    ids = list(pool)
    if not ids:
        return {"summary": "no speech in the footage", "beats": []}
    brief_terms = set(_tokens(brief.get("text", "")))

    def hook_rank(sid: str) -> tuple:
        overlap = len(brief_terms & set(_tokens(pool[sid]["text"] + " " + pool[sid]["caption"])))
        return (overlap, pool[sid]["score"])

    hook_max = style["hook"]["max_s"]
    candidates = [s for s in ids if lengths.get(s, 0) <= hook_max] or ids
    hook = max(candidates, key=hook_rank) if style["hook"]["required"] else None
    rest = [s for s in ids if s != hook]

    cta: list[str] = []
    if rest and set(_tokens(pool[rest[-1]]["text"])) & CTA_WORDS:
        cta = [rest.pop()]

    shares = {b["role"]: b["share"] for b in style["beats"]}
    body_roles = [r for r in ("setup", "body", "payoff") if r in shares]
    total = sum(lengths.get(s, 0) for s in rest) or 1.0
    weight = sum(shares[r] for r in body_roles)
    groups: dict[str, list[str]] = {r: [] for r in body_roles}
    acc = 0.0
    bounds = []
    cum = 0.0
    for r in body_roles:
        cum += shares[r] / weight
        bounds.append((r, cum))
    for sid in rest:
        mid = (acc + lengths.get(sid, 0) / 2) / total
        role = next((r for r, edge in bounds if mid <= edge + 1e-9), body_roles[-1])
        groups[role].append(sid)
        acc += lengths.get(sid, 0)

    energy = {"hook": "high", "setup": "mid", "body": "mid", "payoff": "high", "cta": "low"}
    beats = []
    if hook:
        beats.append({"role": "hook", "title": _title(pool[hook]["text"]),
                      "intent": "open on the strongest line", "segment_ids": [hook]})
    for r in body_roles:
        if groups[r]:
            beats.append({"role": r, "title": _title(pool[groups[r][0]]["text"]),
                          "intent": {"setup": "frame the problem", "body": "deliver the substance",
                                     "payoff": "land the conclusion"}[r],
                          "segment_ids": groups[r]})
    if cta:
        beats.append({"role": "cta", "title": _title(pool[cta[0]]["text"]),
                      "intent": "close and point onwards", "segment_ids": cta})
    for b in beats:
        b["emphasis"] = _emphasis(" ".join(pool[s]["text"] for s in b["segment_ids"]))
        b["music_energy"] = energy[b["role"]]
    return {"summary": f"{len(ids)} sentences in story order, hook moved to the front",
            "beats": beats}


def _title(text: str, n: int = 6) -> str:
    words = text.split()
    return " ".join(words[:n]).rstrip(".,") + ("…" if len(words) > n else "")


PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "beats"],
    "properties": {
        "summary": {"type": "string"},
        "beats": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["role", "title", "intent", "segments", "emphasis", "music_energy"],
            "properties": {
                "role": {"type": "string", "enum": list(ROLES)},
                "title": {"type": "string"},
                "intent": {"type": "string"},
                "segments": {"type": "array", "items": {"type": "string"}},
                "emphasis": {"type": "array", "items": {"type": "string"}},
                "music_energy": {"type": "string", "enum": list(ENERGY)},
            }}},
    },
}

SYSTEM_PROMPT = """You are the edit planner in a video editor. You never see footage; you read a \
contact sheet: one line per sentence the creator spoke (id, time range, highlight score 0-1, \
transcript, scene) and one line per B-roll shot.

Write an Edit Plan: an ordered list of beats. Each beat has a role (hook, setup, body, payoff, \
cta), a short title, its intent, the sentence ids that serve it in play order, up to three \
emphasis words that appear in those sentences, and the music energy under it.

Rules:
- Use only ids from the contact sheet. Never use an id twice. B-roll shot ids are not sentences; \
do not put them in beats.
- Within a beat keep sentences in the order they were spoken unless the brief asks otherwise.
- The hook comes first, runs at most {hook_max} seconds and must make a viewer stay.
- Pick more material than the target length; a solver trims to length by dropping \
the lowest-scoring sentences. Prefer complete thoughts over fragments.
- Style "{style_name}": {style_desc} Beat shares of the length: {shares}.
- Locked beats are fixed by the creator: they will be re-inserted unchanged; do not reuse \
their sentences.
- Call tools when you need detail. Call ask_user only when the brief and the footage \
genuinely disagree; you have {questions_left} questions left.
When you have finished researching, reply with the plan as JSON only."""


def _llm_messages(brief: dict, style: dict, sheet: str, locked: list[dict],
                  answers: dict, questions_left: int, aliases: Aliases, usable_s: float) -> list:
    shares = ", ".join(f"{b['role']} {b['share']:.0%}" for b in style["beats"])
    system = SYSTEM_PROMPT.format(hook_max=style["hook"]["max_s"], style_name=style["name"],
                                  style_desc=style["description"], shares=shares,
                                  questions_left=questions_left)
    target = brief["target_s"]
    lines = [f"Brief: {brief['text'] or '(none given)'}",
             f"Target length: {_mmss(target) if target else 'none, keep what is good'} "
             f"(±{brief['tolerance']:.0%}). Usable speech after jump cuts: {_mmss(usable_s)}.",
             f"Captions: {brief['captions']['mode']}. Music: {brief['music']['mode']} "
             f"{brief['music'].get('mood', '')}."]
    if locked:
        lines.append("Locked beats: " + "; ".join(
            f"{b['role']} '{b['title']}': {', '.join(aliases.short(s) for s in b['segment_ids'])}"
            for b in locked))
    if answers:
        lines.append("Creator's answers: " + "; ".join(f"{q} -> {a}" for q, a in answers.items()))
    lines.append("\nContact sheet:\n" + sheet)
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def _from_llm(raw: dict, aliases: Aliases) -> list[dict]:
    beats = []
    for b in raw.get("beats", []):
        beats.append({"role": b.get("role"), "title": str(b.get("title", ""))[:80],
                      "intent": str(b.get("intent", ""))[:200],
                      "segment_ids": [aliases.long(s) for s in b.get("segments", [])],
                      "emphasis": [str(w).lower() for w in b.get("emphasis", [])][:3],
                      "music_energy": b.get("music_energy") if b.get("music_energy") in ENERGY
                      else "mid"})
    return beats


def plan_llm(ep: llm.Endpoint, brief: dict, style: dict, index: dict, locked: list[dict],
             answers: dict, questions_left: int, lengths: dict[str, float],
             log: Log = print) -> tuple[dict, list[str]]:
    """Story pass: tool loop, then schema-constrained JSON, validated, one repair round."""
    aliases = Aliases(index)
    tools = PlannerTools(index, aliases, questions_left)
    usable_s = sum(lengths.values())
    messages = _llm_messages(brief, style, contact_sheet(index, aliases, usable=lengths), locked,
                             answers, questions_left, aliases, usable_s)
    for _ in range(MAX_TOOL_ROUNDS):
        reply = llm.chat(ep, messages, tools=PlannerTools.SPECS, max_tokens=2048)
        calls = reply.get("tool_calls") or []
        if not calls:
            break
        messages.append({"role": "assistant", "content": reply.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = tools.call(fn.get("name", ""), args)
            log(f"planner tool {fn.get('name')}({json.dumps(args)[:60]})")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": result})

    pool = segment_pool(index, lengths)
    messages.append({"role": "user", "content": "Reply now with the Edit Plan as JSON."})
    for attempt in range(2):
        reply = llm.chat(ep, messages, schema=PLAN_SCHEMA, schema_name="edit_plan",
                         max_tokens=4096, temperature=0.2)
        try:
            raw = llm.parse_json(reply.get("content"))
            beats = _from_llm(raw, aliases)
            for n, b in enumerate(beats):
                b["id"] = f"b{n + 1}"
            errors = validate(beats, pool, [])
        except (ValueError, json.JSONDecodeError) as exc:
            raw, beats, errors = {}, [], [f"not valid JSON: {exc}"]
        if not errors:
            return {"summary": str(raw.get("summary", ""))[:400], "beats": beats}, tools.asked
        log(f"planner output rejected ({len(errors)} errors), asking for a repair")
        messages.append({"role": "assistant", "content": reply.get("content") or ""})
        messages.append({"role": "user", "content": "That plan is invalid:\n- " + "\n- ".join(
            errors[:10]) + "\nReply with a corrected plan as JSON only."})
    raise llm.LLMError("planner produced no valid plan after a repair round")


# --- the plan document --------------------------------------------------------------

def length_question(brief: dict, usable_s: float) -> str | None:
    """The disagreement the plan names (§2): more length asked for than the footage holds."""
    target = brief.get("target_s")
    if target and usable_s < target * (1 - brief["tolerance"]):
        return (f"You asked for {_mmss(target)} but the footage holds {_mmss(usable_s)} of usable "
                f"speech after jump cuts. Ship at {_mmss(usable_s)}, or give a shorter target "
                f"(e.g. \"2:30\")?")
    return None


def _apply_length_answer(brief: dict, answer: str) -> dict:
    m = re.search(r"(\d+):(\d{2})", answer)
    if m:
        return {**brief, "target_s": int(m.group(1)) * 60 + int(m.group(2))}
    return {**brief, "target_s": None}  # "ship it": keep everything usable


def latest_plan(project: Project, with_beats: bool = False) -> dict | None:
    latest = project.latest("plans", "edit_plan", ".json")
    if not latest:
        return None
    if not with_beats:
        return read_json(latest[1])
    for v in range(latest[0], 0, -1):
        path = project.path("plans", f"edit_plan.v{v}.json")
        if path.exists() and (doc := read_json(path)).get("beats"):
            return doc
    return None


def _questions_asked(project: Project) -> list[str]:
    path = project.path("plans", "questions.json")
    return read_json(path) if path.exists() else []


def make_plan(project: Project, answers: dict | None = None, log: Log = print) -> dict:
    index = project.clip_index()
    brief = load_brief(project)
    style = styles.for_brief(brief["style"], brief)
    lengths = segment_lengths(index, style)
    usable = sum(lengths["body"].values())
    # A clarifying question saves a plan without beats; locks live on the last real one.
    previous = latest_plan(project, with_beats=True)
    locked = [b for b in (previous or {}).get("beats", []) if b.get("locked")]
    asked = _questions_asked(project)
    answers = answers or {}

    q = length_question(brief, usable)
    if q and q in answers:
        brief = save_brief(project, _apply_length_answer(brief, answers[q]))
    elif q and (q in asked or len(asked) < MAX_QUESTIONS):
        # An unanswered question comes back without spending the budget again.
        if q not in asked:
            write_json(project.path("plans", "questions.json"), asked + [q])
        return _save(project, {"planner": "precheck", "style": style["id"], "summary": "",
                               "questions": [{"id": "q1", "text": q}], "beats": [],
                               "answers": answers, "brief": brief}, None, lengths, log)

    ep = llm.planner_endpoint()
    planner, questions = "heuristic", []
    raw = None
    if ep is not None and llm.ping(ep):
        try:
            raw, questions = plan_llm(ep, brief, style, index, locked, answers,
                                      MAX_QUESTIONS - len(asked), lengths["body"], log)
            planner = f"llm:{ep.model}"
        except llm.LLMError as exc:
            log(f"LLM planner failed, falling back to the deterministic planner: {exc}")
    elif ep is not None:
        log(f"LLM endpoint {ep.url} not reachable: deterministic planner")
    if raw is None:
        raw = plan_heuristic(brief, style, index, lengths["body"])

    for n, b in enumerate(raw["beats"]):
        b.setdefault("id", f"b{n + 1}")
    beats = _merge_locked(raw["beats"], locked, (previous or {}).get("beats", []))
    _renumber(beats, locked)
    if questions:
        write_json(project.path("plans", "questions.json"), asked + questions)
    doc = {"planner": planner, "style": style["id"], "summary": raw.get("summary", ""),
           "questions": [{"id": f"q{n + 1}", "text": t} for n, t in enumerate(questions)],
           "beats": beats, "answers": answers, "brief": brief,
           "excluded_ids": (previous or {}).get("excluded_ids", []),
           "pinned_ids": (previous or {}).get("pinned_ids", [])}
    return _save(project, doc, style, lengths, log)


def _renumber(beats: list[dict], locked: list[dict]) -> None:
    taken = {b["id"] for b in locked}
    n = 1
    for b in beats:
        if b["id"] in taken and b in locked:
            continue
        while f"b{n}" in taken:
            n += 1
        b["id"] = f"b{n}"
        taken.add(b["id"])
        b.setdefault("locked", False)


def edit_plan(project: Project, edits: list[dict], log: Log = print) -> dict:
    """Apply storyboard edits: order, deletions, locks, removed segments. No LLM involved."""
    current = latest_plan(project)
    if current is None:
        raise ValueError("no plan to edit")
    index = project.clip_index()
    pool = segment_pool(index)
    by_id = {b["id"]: b for b in current["beats"]}
    beats = []
    for e in edits:
        if e["id"] not in by_id:
            raise ValueError(f"unknown beat {e['id']}")
        b = dict(by_id[e["id"]])
        b["locked"] = bool(e.get("locked", b.get("locked", False)))
        if "segment_ids" in e:
            unknown = [s for s in e["segment_ids"] if s not in pool]
            if unknown:
                raise ValueError(f"unknown segments: {', '.join(unknown)}")
            b["segment_ids"] = [s for s in e["segment_ids"]]
            b["dropped_ids"] = []
        else:
            b["segment_ids"] = b["segment_ids"] + b.get("dropped_ids", [])
        beats.append(b)
    errors = validate(beats, pool, [])
    if errors:
        raise ValueError("; ".join(errors))
    from . import learn
    learn.from_plans(project.id, index, current["beats"], beats, "storyboard")
    brief = load_brief(project)
    style = styles.for_brief(current["style"], brief)
    doc = {**current, "beats": beats, "questions": [], "planner": current["planner"],
           "brief": brief}
    return _save(project, doc, style, segment_lengths(index, style), log, edited=True)


def _save(project: Project, doc: dict, style: dict | None, lengths: dict, log: Log,
          edited: bool = False, persist: bool = True) -> dict:
    brief = doc.get("brief") or load_brief(project)
    sol = None
    excluded = set(doc.get("excluded_ids", []))
    if doc["beats"] and style is not None:
        index = project.clip_index()
        pool = segment_pool(index)
        seg_len = {}
        for b in doc["beats"]:
            b["dropped_ids"] = [s for s in b.get("dropped_ids", []) if s not in excluded]
            # Re-plans and edits bring back every sentence the beat was given;
            # the solver decides afresh what survives.
            # A locked beat is exactly what the creator saw: no resurrections.
            ids = b["segment_ids"] + ([] if edited or b.get("locked")
                                      else b.get("dropped_ids", []))
            b["segment_ids"] = [s for s in dict.fromkeys(ids) if s not in excluded]
            table = lengths["hook"] if b["role"] == "hook" else lengths["body"]
            for s in b["segment_ids"]:
                seg_len[s] = table.get(s, 0.0)
        target = brief.get("target_s")
        shares = {x["role"]: x["share"] for x in style["beats"]}
        role_count = Counter(b["role"] for b in doc["beats"])
        beats_in = [solver.BeatIn(b["id"], b["role"], b["segment_ids"],
                                  target * shares.get(b["role"], 0) / role_count[b["role"]]
                                  if target else None, b.get("locked", False))
                    for b in doc["beats"]]
        sol = solver.solve(beats_in, seg_len, {s: pool[s]["score"] for s in pool}, target,
                           brief["tolerance"], style["hook"]["max_s"],
                           must_keep=set(doc.get("pinned_ids", [])))
        for b, bi in zip(doc["beats"], beats_in):
            b["dropped_ids"] = sol.dropped[b["id"]]
            b["segment_ids"] = sol.kept[b["id"]]
            b["duration_s"] = round(sum(seg_len[s] for s in b["segment_ids"]), 2)
            b["target_s"] = round(bi.target_s, 2) if bi.target_s else None
            b.setdefault("emphasis", [])
            b.setdefault("music_energy", "mid")
            b.setdefault("locked", False)
        log(f"solver: {sol.kept_s:.1f} s kept, {sol.status}")
    doc["solver"] = {"target_s": brief.get("target_s"), "kept_s": sol.kept_s if sol else 0.0,
                     "status": sol.status if sol else "no_target",
                     "shortfall_s": sol.shortfall_s if sol else 0.0,
                     "notes": sol.notes if sol else []}
    doc["brief_hash"] = stable_hash(brief)
    if not persist:
        doc["version"] = 0
        return doc
    version, path = project.next_version("plans", "edit_plan", ".json")
    doc["version"] = version
    write_json(path, doc)
    return doc


def restore_version(project: Project, version: int) -> dict:
    """An earlier plan, saved again as the newest version (undo never rewrites history)."""
    doc = read_json(project.path("plans", f"edit_plan.v{version}.json"))
    new_version, path = project.next_version("plans", "edit_plan", ".json")
    doc = {**doc, "version": new_version, "restored_from": version}
    write_json(path, doc)
    return doc


def trial_plan(project: Project, style_id: str, log: Log = print) -> dict:
    """The current plan's content under another style, solved but never saved (plan §5:
    "try both" compares styles on the same story, not two different edits)."""
    from copy import deepcopy

    index = project.clip_index()
    brief = {**load_brief(project), "style": style_id, "overrides": {}}
    style = styles.for_brief(style_id, brief)
    lengths = segment_lengths(index, style)
    base = latest_plan(project, with_beats=True)
    if base:
        doc = deepcopy(base)
        for b in doc["beats"]:
            b["segment_ids"] = b["segment_ids"] + b.get("dropped_ids", [])
            b["dropped_ids"] = []
            b["locked"] = False  # locks bind the real plan, not a trial
    else:
        doc = plan_heuristic(brief, style, index, lengths["body"])
        for n, b in enumerate(doc["beats"]):
            b["id"] = f"b{n + 1}"
    doc.update({"planner": "trial", "style": style_id, "questions": [], "brief": brief})
    return _save(project, doc, style, lengths, log, persist=False)


def plan_hash(doc: dict) -> str:
    keep = {k: doc[k] for k in ("style", "beats", "brief") if k in doc}
    return hashlib.blake2b(json.dumps(keep, sort_keys=True).encode(), digest_size=8).hexdigest()
