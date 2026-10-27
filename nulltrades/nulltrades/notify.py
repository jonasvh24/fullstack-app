"""Telegram alerts. Every message carries the mode label, so a paper result can
never read as real money."""

from __future__ import annotations

import logging

import httpx

from .models import Mode

log = logging.getLogger("nulltrades.notify")
# httpx logs full request URLs at INFO, and a Bot API URL contains the token.
logging.getLogger("httpx").setLevel(logging.WARNING)


def mode_label(mode: Mode) -> str:
    return "[PAPER · SIMULATED]" if mode == Mode.PAPER else "[LIVE · REAL MONEY]"


class Notifier:
    """Base notifier: logs only. Used when Telegram is not configured."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, mode: Mode, text: str) -> None:
        msg = f"{mode_label(mode)} {text}"
        self.sent.append(msg)
        log.info("notify: %s", msg)


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, enabled: bool = True) -> None:
        super().__init__()
        self._token = token
        self._chat = chat_id
        self.enabled = enabled

    async def send(self, mode: Mode, text: str) -> None:
        await super().send(mode, text)
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json={"chat_id": self._chat, "text": f"{mode_label(mode)} {text}"})
                if r.status_code != 200:
                    log.warning("telegram send failed: HTTP %s", r.status_code)
        except httpx.HTTPError as e:
            # Exception text can include the URL; strip the token before logging.
            log.warning("telegram send failed: %s", str(e).replace(self._token, "<token>"))


def make_notifier(secrets, enabled: bool) -> Notifier:
    if secrets.telegram_token and secrets.telegram_chat:
        return TelegramNotifier(secrets.telegram_token, secrets.telegram_chat, enabled)
    return Notifier()
