"""Public-profile availability check.

We fetch https://www.tiktok.com/@<name> and read the JSON TikTok embeds for
its own front end. Three outcomes:

    TAKEN       the page carries a user object: someone holds the handle.
    NO_PROFILE  TikTok answered cleanly with no user. The handle is a
                candidate, not a guarantee: banned, deleted and reserved
                handles also have no public profile but cannot be claimed.
                Confirm in the app (Edit profile > Username) before relying
                on it.
    UNKNOWN     anything else: bot wall, captcha, layout change, network
                error. Never counted as available.

The checker is deliberately polite: one request at a time, a delay with
jitter between requests, exponential backoff on 429, and a hard stop when
TikTok keeps refusing. It does not rotate proxies or solve captchas; if
TikTok blocks you, slow down or stop.
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Callable

PROFILE_URL = "https://www.tiktok.com/@{name}"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

_SCRIPT_RE = re.compile(
    r'<script[^>]*id="(__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE)"[^>]*>(.*?)</script>',
    re.S,
)


class Status(str, Enum):
    TAKEN = "taken"
    NO_PROFILE = "no_profile"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    username: str
    status: Status
    http_status: int | None = None
    tiktok_code: int | None = None
    detail: str = ""


class Blocked(RuntimeError):
    """Raised after too many consecutive unusable responses."""

    def __init__(self, message: str, result: "CheckResult"):
        super().__init__(message)
        self.result = result


def parse_profile_html(username: str, html: str) -> tuple[Status, int | None, str]:
    match = _SCRIPT_RE.search(html)
    if not match:
        return Status.UNKNOWN, None, "no embedded data (bot wall or layout change)"

    kind, raw = match.groups()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Status.UNKNOWN, None, "embedded data is not valid JSON"

    if kind == "SIGI_STATE":
        users = (data.get("UserModule") or {}).get("users") or {}
        if any(k.lower() == username for k in users):
            return Status.TAKEN, 0, ""
        return Status.NO_PROFILE, None, "legacy layout, user absent"

    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.user-detail")
    if not isinstance(detail, dict):
        return Status.UNKNOWN, None, "user-detail block missing"

    code = detail.get("statusCode")
    user = ((detail.get("userInfo") or {}).get("user")) or {}
    unique_id = str(user.get("uniqueId", "")).lower()

    # Private accounts still return a user object, so presence of the user
    # is the test, not the status code.
    if unique_id:
        if unique_id == username:
            return Status.TAKEN, code, ""
        return Status.UNKNOWN, code, f"page returned a different user: {unique_id}"
    if code not in (None, 0):
        return Status.NO_PROFILE, code, detail.get("statusMsg") or ""
    return Status.UNKNOWN, code, "status 0 but no user object"


Fetcher = Callable[[str], tuple[int, str]]


def http_fetch(url: str, timeout: float = 15.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", errors="replace")


class Checker:
    def __init__(
        self,
        delay: float = 3.0,
        jitter: float = 1.5,
        max_retries: int = 3,
        max_consecutive_unknown: int = 5,
        fetch: Fetcher = http_fetch,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.delay = delay
        self.jitter = jitter
        self.max_retries = max_retries
        self.max_consecutive_unknown = max_consecutive_unknown
        self._fetch = fetch
        self._sleep = sleep
        self._consecutive_unknown = 0
        self._last_request = 0.0

    def _pace(self) -> None:
        wait = self.delay + random.uniform(0, self.jitter) - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def check(self, username: str) -> CheckResult:
        result = self._check_once(username)
        if result.status is Status.UNKNOWN:
            self._consecutive_unknown += 1
            if self._consecutive_unknown >= self.max_consecutive_unknown:
                raise Blocked(
                    f"{self._consecutive_unknown} unusable responses in a row "
                    f"(last: {result.detail}). TikTok is blocking or unreachable; "
                    "wait, then rerun with a longer --delay.",
                    result,
                )
        else:
            self._consecutive_unknown = 0
        return result

    def _check_once(self, username: str) -> CheckResult:
        backoff = max(self.delay, 1.0) * 4
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                http_status, body = self._fetch(PROFILE_URL.format(name=username))
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                return CheckResult(username, Status.UNKNOWN, detail=f"network: {err}")

            if http_status == 429 and attempt < self.max_retries:
                self._sleep(backoff)
                backoff *= 2
                continue
            if http_status == 404:
                return CheckResult(username, Status.NO_PROFILE, http_status, None, "http 404")
            if http_status != 200:
                return CheckResult(username, Status.UNKNOWN, http_status, None, f"http {http_status}")

            status, code, detail = parse_profile_html(username, body)
            return CheckResult(username, status, http_status, code, detail)

        return CheckResult(username, Status.UNKNOWN, 429, None, "rate limited after retries")
