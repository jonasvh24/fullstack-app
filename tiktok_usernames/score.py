"""Rarity scoring, 0-100.

Length dominates: the supply of 3-letter handles is 17,576 in total, so
shortness is what makes a handle scarce. Clean letters-only names, names
you can say out loud, and real words add on top; digits and separators
take away, because they are how people work around a taken name.
"""
from __future__ import annotations

from .generate import VOWELS

_LENGTH_POINTS = {2: 60, 3: 50, 4: 38, 5: 26, 6: 16, 7: 8}


def is_pronounceable(name: str) -> bool:
    if not name.isalpha() or len(name) < 2:
        return False
    if not any(ch in VOWELS for ch in name):
        return False
    run_c = run_v = 0
    for ch in name:
        if ch in VOWELS:
            run_v, run_c = run_v + 1, 0
        else:
            run_c, run_v = run_c + 1, 0
        if run_c > 2 or run_v > 2:
            return False
    return True


def rarity(name: str, wordset: set[str] | None = None) -> int:
    score = _LENGTH_POINTS.get(len(name), 0)

    if name.isalpha():
        score += 15
    else:
        digits = sum(ch.isdigit() for ch in name)
        separators = sum(ch in "._" for ch in name)
        score -= min(digits * 5, 15) + separators * 10

    if is_pronounceable(name):
        score += 10
    if wordset and name in wordset:
        score += 15
    if len(set(name)) == 1 or (len(name) >= 3 and name == name[::-1]):
        score += 5

    return max(0, min(100, score))
