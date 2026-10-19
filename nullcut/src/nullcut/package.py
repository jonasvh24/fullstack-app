"""Titles, description and chapters, written last from the finished edit (plan §10).

Titles describe the video that exists, not the one the brief imagined. Every
factual or promissory claim in a title must map to the transcript; a title
the video does not pay off is dropped, whoever wrote it.
"""

from __future__ import annotations

import re

from . import llm

TITLE_MAX = 60
CHAPTER_MIN_S = 10.0
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "that", "this",
        "we", "i", "you", "for", "with", "how", "why", "what", "my", "your", "our", "de",
        "het", "een", "en", "van", "hoe", "waarom", "wat", "mijn", "jouw"}


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def chapters(beats: list[dict], duration: float) -> list[dict]:
    """YouTube's rules: first stamp at 0:00, at least three chapters, each at least 10 s.

    Short beats merge into the chapter before them; fewer than three chapters
    after merging means none at all, because YouTube would ignore them anyway.
    """
    merged: list[dict] = []
    for b in beats:
        if merged and merged[-1]["end"] - merged[-1]["t"] < CHAPTER_MIN_S:
            merged[-1]["end"] = b["end_s"]  # the open chapter is too short: absorb this beat
            continue
        merged.append({"t": 0.0 if not merged else round(b["start_s"], 2), "title": b["title"],
                       "end": b["end_s"]})
    if len(merged) > 1 and merged[-1]["end"] - merged[-1]["t"] < CHAPTER_MIN_S:
        merged[-2]["end"] = merged[-1]["end"]
        merged.pop()
    return [{"t": c["t"], "title": c["title"]} for c in merged] if len(merged) >= 3 else []


def _content_terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w']+", text.lower()) if w not in STOP and len(w) > 2]


def verify(title: str, transcript: str) -> bool:
    """Numbers must appear verbatim; content words must appear at least as a stem."""
    spoken = transcript.lower()
    for num in re.findall(r"\d+", title):
        if not re.search(rf"\b{num}\b", spoken):
            return False
    stems = {w[:5] for w in re.findall(r"[\w']+", spoken)}
    terms = _content_terms(title)
    if not terms:
        return True
    hits = sum(1 for t in terms if t[:5] in stems)
    # Allow framing words ("guide", "explained") but most of the substance must be said.
    return hits / len(terms) >= 0.6


def title_pattern(refs: str) -> dict | None:
    """Plan §10 step 1: what the creator's own titles have in common."""
    titles = [t.strip() for t in (refs or "").splitlines() if t.strip()]
    if len(titles) < 3:
        return None
    words = [re.findall(r"[\w']+", t) for t in titles]
    def caps(t: str) -> str:
        letters = [c for c in t if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
            return "upper"
        ws = [w for w in re.findall(r"[A-Za-z']+", t) if len(w) > 3]
        return "title" if ws and sum(w[0].isupper() for w in ws) / len(ws) > 0.7 else "sentence"
    styles_ = [caps(t) for t in titles]
    return {"count": len(titles),
            "chars": round(sum(len(t) for t in titles) / len(titles)),
            "words": round(sum(len(w) for w in words) / len(words), 1),
            "numbers": round(sum(bool(re.search(r"\d", t)) for t in titles) / len(titles), 2),
            "questions": round(sum(t.endswith("?") for t in titles) / len(titles), 2),
            "caps": max(set(styles_), key=styles_.count)}


def apply_caps(text: str, caps: str) -> str:
    if caps == "upper":
        return text.upper()
    if caps == "title":
        return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in text.split())
    return text[:1].upper() + text[1:]


def _heuristic_titles(beats: list[dict], sentences: dict[str, dict]) -> list[dict]:
    """Lines from the finished edit only, and only ones that say something."""
    out = []
    kept = {sid for b in beats for sid in b["segment_ids"]}
    sentences = {k: v for k, v in sentences.items()
                 if k in kept and len(_content_terms(v["text"])) >= 2}
    order = sorted(sentences.values(), key=lambda s: -s["score"])
    hook_ids = [sid for b in beats if b["role"] == "hook" for sid in b["segment_ids"]]
    for s in [sentences[i] for i in hook_ids if i in sentences] + order:
        text = s["text"].strip().rstrip(".")
        if len(text) > TITLE_MAX:
            text = text[:TITLE_MAX].rsplit(" ", 1)[0] + "…"
        if text and text not in {t["text"] for t in out}:
            out.append({"text": text, "rationale": "the edit's own words, highest-scoring line"
                        if s["id"] not in hook_ids else "the hook line, verbatim"})
        if len(out) == 5:
            break
    return out


TITLE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["titles", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "titles": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "rationale"],
            "properties": {"text": {"type": "string"}, "rationale": {"type": "string"}}}},
    },
}


def _llm_titles(ep: llm.Endpoint, brief: dict, beats: list[dict], transcript: str,
                language: str) -> tuple[list[dict], str]:
    outline = "\n".join(f"- {b['role']}: {b['title']} ({b.get('intent', '')})" for b in beats)
    pattern = title_pattern(brief.get("title_refs", ""))
    refs = ""
    if pattern:
        refs = (f"\n\nThe creator's own titles, to match in pattern (not in content):\n"
                f"{brief['title_refs'].strip()}\nMeasured pattern: about {pattern['chars']} "
                f"characters, numbers in {pattern['numbers']:.0%}, questions in "
                f"{pattern['questions']:.0%}, {pattern['caps']} case.")
    prompt = (f"Write ten YouTube titles for this finished video, best first, in "
              f"{'Dutch' if language == 'nl' else 'English'}. Under {TITLE_MAX} characters, "
              f"hook in the first half, no clickbait the video does not pay off, no emojis. "
              f"Give a one-line rationale each. Also a two-sentence summary for the "
              f"description.\n\nBrief: {brief.get('text') or '(none)'}\n\nOutline:\n{outline}"
              f"{refs}\n\nTranscript of the edit:\n{transcript[:12000]}")
    reply = llm.chat(ep, [{"role": "user", "content": prompt}], schema=TITLE_SCHEMA,
                     schema_name="titles", max_tokens=1500, temperature=0.7)
    raw = llm.parse_json(reply.get("content"))
    titles = [{"text": str(t["text"]).strip()[:TITLE_MAX + 20],
               "rationale": str(t.get("rationale", ""))[:200]} for t in raw.get("titles", [])]
    return titles, str(raw.get("summary", ""))[:500]


def build(brief: dict, beats: list[dict], words: list[dict], sentences: dict[str, dict],
          duration: float, music_doc: dict | None, language: str) -> dict:
    transcript = " ".join(w["w"] for w in words)
    ep = llm.planner_endpoint()
    titles, summary, source = [], "", "heuristic"
    if ep is not None and llm.ping(ep):
        try:
            titles, summary = _llm_titles(ep, brief, beats, transcript, language)
            source = f"llm:{ep.model}"
        except (llm.LLMError, ValueError):
            titles = []
    if not titles:
        titles = _heuristic_titles(beats, sentences)
        pattern = title_pattern(brief.get("title_refs", ""))
        if pattern:
            # Offline titles cannot invent a pattern, but they can wear its case and be
            # ranked by how close they sit to its length.
            for t in titles:
                t["text"] = apply_caps(t["text"], pattern["caps"])
            titles.sort(key=lambda t: abs(len(t["text"]) - pattern["chars"]))
    checked = []
    for t in titles:
        ok = len(t["text"]) <= TITLE_MAX and verify(t["text"], transcript)
        checked.append({**t, "verified": ok})
    # Plan §10: a title the video does not pay off is dropped.
    ranked = [t for t in checked if t["verified"]][:5]
    if not summary:
        hook = next((b for b in beats if b["role"] == "hook"), beats[0] if beats else None)
        payoff = next((b for b in beats if b["role"] == "payoff"), None)
        parts = [sentences[s]["text"] for b in (hook, payoff) if b for s in b["segment_ids"][:1]
                 if s in sentences]
        summary = " ".join(parts)
    chaps = chapters(beats, duration)
    lines = [summary.strip(), ""]
    if chaps:
        lines += [f"{_mmss(c['t'])} {c['title']}" for c in chaps] + [""]
    if music_doc:
        lines.append(f"Music: {music_doc['name']} ({music_doc['licence']})")
    return {"titles": ranked, "rejected_titles": [t for t in checked if not t["verified"]],
            "title_source": source, "description": "\n".join(lines).strip(),
            "chapters": chaps}
