"""Candidate assembly shared by the CLI and the web UI."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import generate, words as wordlists
from .rules import is_valid, normalise
from .score import rarity


@dataclass
class Spec:
    patterns: list[str] = field(default_factory=list)
    lengths: list[int] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    repeats: bool = False
    names: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)  # keys of words.THEMES
    singles: bool = True   # theme and pasted words on their own
    combos: bool = False   # two words joined, e.g. "voidfox"
    max_len: int = 10      # longest single word or combo to keep
    limit: int = 200
    min_rarity: int = 0
    seed: int | None = None


def build(spec: Spec) -> list[tuple[str, int]]:
    """Scored, de-duplicated candidates, rarest first."""
    out: list[str] = []
    for pattern in spec.patterns + ["L" * n for n in spec.lengths]:
        out += generate.from_pattern(pattern, spec.limit, spec.seed)

    words = [w for w in (normalise(w) for w in spec.words) if w.isalpha() and is_valid(w)]
    words = list(dict.fromkeys(wordlists.words_for(spec.themes) + words))
    if spec.singles:
        out += [w for w in words if 3 <= len(w) <= spec.max_len]
    if spec.combos:
        out += generate.combos([w for w in words if len(w) >= 2], spec.limit, spec.max_len, spec.seed)
    if spec.repeats:
        out += generate.repeats()
    out += [n for n in (normalise(n) for n in spec.names) if is_valid(n)]

    wordset = set(words)
    scored = [(n, rarity(n, wordset)) for n in dict.fromkeys(out)]
    scored = [s for s in scored if s[1] >= spec.min_rarity]
    # Rarest first, so an interrupted run has already spent its requests well.
    scored.sort(key=lambda s: (-s[1], len(s[0]), s[0]))
    return scored
