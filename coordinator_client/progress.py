"""Crash-safe progress store.

A restart must never re-score a result that was already dealt with.  SQLite
with a synchronous commit per state change gives us that durably: the row is
committed BEFORE the expensive re-score starts and again when the verdict is
accepted, so a kill at any point leaves a correct record.

States
------
claimed   we have taken the item and are (or were) working on it
submitted a verdict or validator_error was accepted by the coordinator — terminal
failed     local attempt cap reached — terminal, never retried
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
    result_id     INTEGER PRIMARY KEY,
    state         TEXT    NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    molecule_hash TEXT    NOT NULL DEFAULT '',
    outcome       TEXT    NOT NULL DEFAULT '',
    updated_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS progress_state ON progress(state);
"""

STATE_CLAIMED = "claimed"
STATE_SUBMITTED = "submitted"
STATE_FAILED = "failed"
TERMINAL_STATES = (STATE_SUBMITTED, STATE_FAILED)


class ProgressStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ── queries ─────────────────────────────────────────────────────────────
    def state_of(self, result_id: int) -> str | None:
        row = self._db.execute(
            "SELECT state FROM progress WHERE result_id=?", (result_id,)
        ).fetchone()
        return row[0] if row else None

    def attempts(self, result_id: int) -> int:
        row = self._db.execute(
            "SELECT attempts FROM progress WHERE result_id=?", (result_id,)
        ).fetchone()
        return row[0] if row else 0

    def is_done(self, result_id: int) -> bool:
        """True when this result must never be scored again."""
        return self.state_of(result_id) in TERMINAL_STATES

    # ── transitions (each one commits immediately) ──────────────────────────
    def claim(self, result_id: int, molecule_hash: str = "") -> int:
        """Mark the item as being worked on; returns the new attempt count.

        Committed BEFORE any scoring work, so a crash mid-score is visible as
        a claimed row with an incremented attempt count.
        """
        self._db.execute(
            "INSERT INTO progress(result_id, state, attempts, molecule_hash, updated_at) "
            "VALUES(?,?,1,?,?) "
            "ON CONFLICT(result_id) DO UPDATE SET "
            "  state=excluded.state, attempts=progress.attempts+1, "
            "  updated_at=excluded.updated_at",
            (result_id, STATE_CLAIMED, molecule_hash, time.time()),
        )
        return self.attempts(result_id)

    def mark_submitted(self, result_id: int, outcome: str) -> None:
        self._set(result_id, STATE_SUBMITTED, outcome)

    def mark_failed(self, result_id: int, outcome: str) -> None:
        self._set(result_id, STATE_FAILED, outcome)

    def _set(self, result_id: int, state: str, outcome: str) -> None:
        self._db.execute(
            "INSERT INTO progress(result_id, state, attempts, outcome, updated_at) "
            "VALUES(?,?,1,?,?) "
            "ON CONFLICT(result_id) DO UPDATE SET "
            "  state=excluded.state, outcome=excluded.outcome, "
            "  updated_at=excluded.updated_at",
            (result_id, state, outcome, time.time()),
        )

    # ── telemetry ───────────────────────────────────────────────────────────
    def counts(self) -> dict[str, int]:
        return {s: n for s, n in self._db.execute(
            "SELECT state, COUNT(*) FROM progress GROUP BY state")}
