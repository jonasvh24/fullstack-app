"""Word-level captions as ASS for libass, plus SRT/VTT sidecars.

Events live in output time. The renderer slices them per item so a change to
one beat re-renders that beat's captions and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .compiler import Item

STYLES = ("clean", "word")


@dataclass
class Event:
    start: float
    end: float
    text: str  # ASS-ready, may contain override tags


def output_words(items: list[Item], fps: float) -> list[dict]:
    """Map every kept word from source time to output time."""
    out, cursor = [], 0
    for it in items:
        t0 = cursor / fps
        t1 = (cursor + it.frames) / fps
        for w in it.words:
            s = t0 + (w["s"] - it.src_in)
            e = t0 + (w["e"] - it.src_in)
            s, e = max(s, t0), min(e, t1)
            if e > s:
                out.append({"w": w["w"], "s": s, "e": e})
        cursor += it.frames
    return out


def group(words: list[dict], style: str) -> list[list[dict]]:
    max_words = config.CAPTION_MAX_WORDS[style]
    max_chars = config.CAPTION_MAX_CHARS[style]
    blocks: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        if current:
            chars = sum(len(x["w"]) + 1 for x in current) + len(w["w"])
            gap = w["s"] - current[-1]["e"]
            span = w["e"] - current[0]["s"]
            ends_sentence = current[-1]["w"].rstrip("\"')”’").endswith((".", "?", "!", "…"))
            if (len(current) >= max_words or chars > max_chars or span > config.CAPTION_MAX_S
                    or gap > config.CAPTION_BREAK_GAP_S or ends_sentence):
                blocks.append(current)
                current = []
        current.append(w)
    if current:
        blocks.append(current)
    return blocks


def escape(text: str) -> str:
    # ASS has no escape for these; swapping is safer than letting a stray
    # brace open an override block mid-caption.
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_events(words: list[dict], style: str) -> list[Event]:
    blocks = group(words, style)
    events: list[Event] = []
    for n, block in enumerate(blocks):
        start = block[0]["s"]
        nxt = blocks[n + 1][0]["s"] if n + 1 < len(blocks) else float("inf")
        # Hold short blocks long enough to read, never over the next block.
        end = min(max(block[-1]["e"], start + 0.6), nxt)
        texts = [escape(w["w"]) for w in block]
        if style == "clean":
            events.append(Event(start, end, " ".join(texts)))
            continue
        upper = [t.upper() for t in texts]
        for i, w in enumerate(block):
            ws = start if i == 0 else w["s"]
            we = block[i + 1]["s"] if i + 1 < len(block) else end
            if we <= ws:
                continue
            line = " ".join(
                f"{{\\c{config.CAPTION_HIGHLIGHT}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t
                for j, t in enumerate(upper))
            events.append(Event(ws, we, line))
    return events


def slice_events(events: list[Event], t0: float, t1: float) -> list[Event]:
    """Events overlapping [t0, t1), shifted to start at zero and clipped."""
    out = []
    for ev in events:
        s, e = max(ev.start, t0), min(ev.end, t1)
        if e - s > 0.005:
            out.append(Event(s - t0, e - t0, ev.text))
    return out


def _ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_document(events: list[Event], width: int, height: int, style: str) -> str:
    # Sizes scale with output height so preview and master frame identically.
    size = round(height * (0.075 if style == "word" else 0.055))
    outline = max(2, round(height * 0.004))
    margin_v = round(height * 0.08)
    margin_h = round(width * 0.08)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{config.CAPTION_FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [f"Dialogue: 0,{_ass_time(e.start)},{_ass_time(e.end)},Default,,0,0,0,,{e.text}"
             for e in events]
    return header + "\n".join(lines) + ("\n" if lines else "")


def _srt_time(t: float, sep: str = ",") -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def plain_cues(words: list[dict]) -> list[Event]:
    """Sidecar cues use clean grouping whatever the burned-in style is."""
    return build_events(words, "clean")


def srt(words: list[dict]) -> str:
    cues = plain_cues(words)
    return "".join(f"{n}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n\n"
                   for n, c in enumerate(cues, 1))


def vtt(words: list[dict]) -> str:
    cues = plain_cues(words)
    return "WEBVTT\n\n" + "".join(
        f"{_srt_time(c.start, '.')} --> {_srt_time(c.end, '.')}\n{c.text}\n\n" for c in cues)
