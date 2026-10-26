"""Candidate assembly shared by the CLI and the web UI."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import generate
from .rules import is_valid, normalise
from .score import rarity


@dataclass
class Spec:
    patterns: list[str] = field(default_factory=list)
    lengths: list[int] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    repeats: bool = False
    names: list[str] = field(default_factory=list)
    limit: int = 200
    min_rarity: int = 0
    seed: int | None = None


def build(spec: Spec) -> list[tuple[str, int]]:
    """Scored, de-duplicated candidates, rarest first."""
    out: list[str] = []
    for pattern in spec.patterns + ["L" * n for n in spec.lengths]:
        out += generate.from_pattern(pattern, spec.limit, spec.seed)

    words = [w for w in (normalise(w) for w in spec.words) if w.isalpha() and is_valid(w)]
    out += [w for w in words if 3 <= len(w) <= 8]
    if spec.repeats:
        out += generate.repeats()
    out += [n for n in (normalise(n) for n in spec.names) if is_valid(n)]

    wordset = set(words)
    scored = [(n, rarity(n, wordset)) for n in dict.fromkeys(out)]
    scored = [s for s in scored if s[1] >= spec.min_rarity]
    # Rarest first, so an interrupted run has already spent its requests well.
    scored.sort(key=lambda s: (-s[1], len(s[0]), s[0]))
    return scored
