"""Prove the checker reads real TikTok profiles before trusting a run.

A handful of accounts certainly exist; a random 18-character string almost
certainly does not. If the real accounts don't come back TAKEN, or the
random one comes back TAKEN, the checker is not reading TikTok correctly
from this connection, and every "available" it reported would be noise.
Costs three requests.
"""
from __future__ import annotations

import random
import string
from dataclasses import dataclass
from pathlib import Path

from .checker import Checker, CheckResult, Status

# Large, long-standing accounts that are vanishingly unlikely to disappear.
KNOWN_TAKEN = ("tiktok", "khaby.lame", "charlidamelio", "mrbeast", "zachking", "bellapoarch")


@dataclass
class Probe:
    expected: Status
    result: CheckResult

    @property
    def ok(self) -> bool:
        return self.result.status is self.expected

    def line(self) -> str:
        r = self.result
        code = f", code {r.tiktok_code}" if r.tiktok_code not in (None, 0) else ""
        why = f" ({r.detail})" if r.detail and not self.ok else ""
        verdict = "PASS" if self.ok else "FAIL"
        return f"{verdict}  @{r.username}: {r.status.value}{code}, expected {self.expected.value}{why}"


@dataclass
class Report:
    probes: list[Probe]
    debug_files: list[str]

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.probes)

    def explanation(self) -> str:
        if self.ok:
            return "Checks are reading live TikTok profiles correctly."
        known = [p for p in self.probes if p.expected is Status.TAKEN]
        if any(p.result.status is Status.UNKNOWN for p in self.probes):
            return ("TikTok is not serving profile pages to this connection (bot check, login wall "
                    "or network block). Results would be meaningless; wait a while, or try another network.")
        if any(p.result.status is Status.NO_PROFILE for p in known):
            return ("Accounts that certainly exist came back as no profile, so TikTok's page format "
                    "has changed and 'available' results would be false. The checker needs updating.")
        return "A random name came back as taken, so the page parser is misreading TikTok. The checker needs updating."

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "explanation": self.explanation(),
            "lines": [p.line() for p in self.probes],
            "debug_files": self.debug_files,
        }


def nonsense_name(rng: random.Random) -> str:
    return "zq" + "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(16))


def verify(checker: Checker, known: int = 2, debug_dir: Path | None = None,
           rng: random.Random | None = None) -> Report:
    rng = rng or random.Random()
    plan = [(n, Status.TAKEN) for n in rng.sample(KNOWN_TAKEN, known)]
    plan.append((nonsense_name(rng), Status.NO_PROFILE))

    probes = [Probe(expected, checker.check_once(name)) for name, expected in plan]

    debug_files = []
    if debug_dir is not None:
        for p in probes:
            if not p.ok and p.result.page:
                debug_dir.mkdir(parents=True, exist_ok=True)
                path = debug_dir / f"{p.result.username}.html"
                path.write_text(p.result.page, encoding="utf-8")
                debug_files.append(str(path))
    return Report(probes, debug_files)
