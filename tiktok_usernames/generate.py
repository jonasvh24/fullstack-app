"""Candidate generators.

Pattern syntax (one token per character):
    C  consonant      V  vowel      L  any letter
    D  digit          A  letter or digit
    anything else (lowercase letters, '_', '.') is kept literally.

So "CVCV" yields names like "bako", "LLL" every three-letter name, and
"xVC" names like "xak".
"""
import itertools
import random

from .rules import is_valid

VOWELS = "aeiou"
CONSONANTS = "bcdfghjklmnpqrstvwxyz"
LETTERS = "abcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"

_TOKENS = {
    "C": CONSONANTS,
    "V": VOWELS,
    "L": LETTERS,
    "D": DIGITS,
    "A": LETTERS + DIGITS,
}


def _pools(pattern: str) -> list[str]:
    return [_TOKENS.get(ch, ch.lower()) for ch in pattern]


def pattern_space(pattern: str) -> int:
    size = 1
    for pool in _pools(pattern):
        size *= len(pool)
    return size


def from_pattern(pattern: str, limit: int, seed: int | None = None) -> list[str]:
    """Return up to `limit` valid names matching `pattern`, in random order.

    Small spaces are enumerated exhaustively and shuffled; large ones are
    sampled so a 6-letter pattern (300m+ names) doesn't get materialised.
    """
    rng = random.Random(seed)
    pools = _pools(pattern)
    size = pattern_space(pattern)

    if size <= max(limit * 4, 50_000):
        names = ["".join(p) for p in itertools.product(*pools)]
        names = [n for n in names if is_valid(n)]
        rng.shuffle(names)
        return names[:limit]

    seen: set[str] = set()
    attempts = 0
    # Bounded so an over-constrained pattern can't spin forever.
    while len(seen) < limit and attempts < limit * 20:
        attempts += 1
        name = "".join(rng.choice(pool) for pool in pools)
        if is_valid(name):
            seen.add(name)
    return list(seen)


def repeats(min_len: int = 3, max_len: int = 5) -> list[str]:
    """Visually striking shapes: aaa, abab, abba, aabb."""
    out: list[str] = []
    for n in range(min_len, max_len + 1):
        out.extend(ch * n for ch in LETTERS)
    for a, b in itertools.permutations(LETTERS, 2):
        out.extend([a + b + a + b, a + b + b + a, a + a + b + b])
    return [n for n in dict.fromkeys(out) if min_len <= len(n) <= max_len]

