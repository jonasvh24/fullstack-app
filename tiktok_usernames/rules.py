"""TikTok username rules.

TikTok handles are case-insensitive, 2-24 characters, and limited to letters,
digits, underscores and periods. A handle cannot end with a period. We also
reject consecutive periods: TikTok refuses them in practice, and checking
names that can never be registered wastes requests.
"""
from __future__ import annotations

import re

MIN_LEN = 2
MAX_LEN = 24

_ALLOWED = re.compile(r"^[a-z0-9._]+$")


def normalise(name: str) -> str:
    return name.strip().lstrip("@").lower()


def is_valid(name: str) -> bool:
    if not MIN_LEN <= len(name) <= MAX_LEN:
        return False
    if not _ALLOWED.match(name):
        return False
    if name.endswith(".") or ".." in name:
        return False
    return True
