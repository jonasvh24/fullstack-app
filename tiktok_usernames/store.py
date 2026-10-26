"""SQLite cache, so reruns skip names already checked and results accumulate."""
import sqlite3
import time
from pathlib import Path

from .checker import CheckResult, Status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    username    TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    rarity      INTEGER NOT NULL,
    http_status INTEGER,
    tiktok_code INTEGER,
    detail      TEXT,
    checked_at  REAL NOT NULL
)
"""


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute(_SCHEMA)

    def is_fresh(self, username: str, max_age_hours: float) -> bool:
        # UNKNOWN results are never treated as fresh: they told us nothing.
        row = self.conn.execute(
            "SELECT checked_at FROM results WHERE username = ? AND status != ?",
            (username, Status.UNKNOWN.value),
        ).fetchone()
        return row is not None and time.time() - row[0] < max_age_hours * 3600

    def save(self, result: CheckResult, rarity: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                result.username,
                result.status.value,
                rarity,
                result.http_status,
                result.tiktok_code,
                result.detail,
                time.time(),
            ),
        )
        self.conn.commit()

    def top(self, status: Status, limit: int, min_rarity: int = 0) -> list[tuple]:
        return self.conn.execute(
            "SELECT username, rarity, tiktok_code, datetime(checked_at, 'unixepoch') "
            "FROM results WHERE status = ? AND rarity >= ? "
            "ORDER BY rarity DESC, length(username), username LIMIT ?",
            (status.value, min_rarity, limit),
        ).fetchall()

    def counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status, COUNT(*) FROM results GROUP BY status"))

    def close(self) -> None:
        self.conn.close()
