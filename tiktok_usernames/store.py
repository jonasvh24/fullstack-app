"""SQLite cache, so reruns skip names already checked and results accumulate."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .checker import CheckResult, Status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    username     TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    rarity       INTEGER NOT NULL,
    http_status  INTEGER,
    tiktok_code  INTEGER,
    detail       TEXT,
    checked_at   REAL NOT NULL,
    confirmed    INTEGER,
    confirmed_at REAL
)
"""

# Columns added after the first release; older databases get them on open.
_MIGRATIONS = {"confirmed": "INTEGER", "confirmed_at": "REAL"}

# Named result views. "Likely" is the automated verdict; "confirmed" and
# "rejected" are what you found when trying the name in the TikTok app.
VIEWS = {
    "likely": "status = 'no_profile' AND confirmed IS NULL",
    "confirmed": "confirmed = 1",
    "rejected": "confirmed = 0",
    "no_profile": "status = 'no_profile'",
    "taken": "status = 'taken'",
    "unknown": "status = 'unknown'",
    "all": "1 = 1",
}


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute(_SCHEMA)
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(results)")}
        for col, kind in _MIGRATIONS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE results ADD COLUMN {col} {kind}")
        self.conn.commit()

    def is_fresh(self, username: str, max_age_hours: float) -> bool:
        # UNKNOWN results are never treated as fresh: they told us nothing.
        row = self.conn.execute(
            "SELECT checked_at FROM results WHERE username = ? AND status != ?",
            (username, Status.UNKNOWN.value),
        ).fetchone()
        return row is not None and time.time() - row[0] < max_age_hours * 3600

    def save(self, result: CheckResult, rarity: int) -> None:
        # A recheck keeps your confirmation, unless the name has since been taken.
        self.conn.execute(
            """
            INSERT INTO results (username, status, rarity, http_status, tiktok_code, detail, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                status = excluded.status,
                rarity = excluded.rarity,
                http_status = excluded.http_status,
                tiktok_code = excluded.tiktok_code,
                detail = excluded.detail,
                checked_at = excluded.checked_at,
                confirmed = CASE WHEN excluded.status = 'taken' THEN NULL ELSE results.confirmed END,
                confirmed_at = CASE WHEN excluded.status = 'taken' THEN NULL ELSE results.confirmed_at END
            """,
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

    def mark(self, username: str, claimable: bool | None) -> bool:
        """Record what the TikTok app said. None clears it. False if the name was never checked."""
        value = None if claimable is None else int(claimable)
        cur = self.conn.execute(
            "UPDATE results SET confirmed = ?, confirmed_at = ? WHERE username = ?",
            (value, None if value is None else time.time(), username),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def rows(self, view: str, min_rarity: int, search: str, limit: int) -> list[dict]:
        sql = f"SELECT * FROM results WHERE ({VIEWS[view]}) AND rarity >= ? AND username LIKE ? ESCAPE '\\'"
        like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += " ORDER BY rarity DESC, length(username), username LIMIT ?"
        cur = self.conn.execute(sql, (min_rarity, like, limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def view_counts(self) -> dict[str, int]:
        return {v: self.conn.execute(f"SELECT COUNT(*) FROM results WHERE {w}").fetchone()[0]
                for v, w in VIEWS.items()}

    def code_stats(self) -> list[dict]:
        """For each TikTok status code, how often a confirmed name was really claimable."""
        rows = self.conn.execute(
            "SELECT tiktok_code, SUM(confirmed = 1), SUM(confirmed = 0) FROM results "
            "WHERE status = 'no_profile' AND confirmed IS NOT NULL "
            "GROUP BY tiktok_code ORDER BY COUNT(*) DESC, tiktok_code"
        ).fetchall()
        return [{"code": c, "claimable": y, "not_claimable": n} for c, y, n in rows]

    def counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status, COUNT(*) FROM results GROUP BY status"))

    def close(self) -> None:
        self.conn.close()
