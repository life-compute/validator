"""Crash-safe progress store.

A restart must never re-score a result that was already dealt with.  SQLite
with a synchronous commit per state change gives us that durably: the row is
committed BEFORE the expensive re-score starts and again when the verdict is
accepted, so a kill at any point leaves a correct record.

States
------
claimed   we have taken the item and are (or were) working on it
submitted a verdict was ACCEPTED by the Worker (2xx), or the Worker answered
          409 "a verdict already exists" — terminal, never re-scored
failed    local attempt cap reached for ONE offer — terminal only for that
          offer; a fresh offer from the Worker starts a new attempt budget

A validator_error and a malformed item are deliberately NOT terminal: the
Worker owns the retry decision (soft-error cooldown + review window), so the
validator must re-process the result whenever the Worker offers it again.

Offer identity
--------------
`offer_tag` is the Worker's own `scoring_attempts` for the row.  It advances
every time the Worker accepts a validator_error, so a changed tag means "this
is a NEW offer" and the local attempt budget resets.
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
    offer_tag     TEXT    NOT NULL DEFAULT '',
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
        self._migrate()

    def _migrate(self) -> None:
        """Add offer_tag to a store written by the pre-fix schema."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(progress)")}
        if "offer_tag" not in cols:
            self._db.execute(
                "ALTER TABLE progress ADD COLUMN offer_tag TEXT NOT NULL DEFAULT ''")

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

    def offer_tag_of(self, result_id: int) -> str:
        row = self._db.execute(
            "SELECT offer_tag FROM progress WHERE result_id=?", (result_id,)
        ).fetchone()
        return (row[0] or "") if row else ""

    def is_done(self, result_id: int, offer_tag: str | None = None) -> bool:
        """True when this result must not be scored again.

        STATE_SUBMITTED is unconditionally terminal: a verdict was accepted
        (or a 409 proved one exists), so the work is finished for good.

        STATE_FAILED is terminal only for the offer that burned the local
        attempt budget.  When the Worker makes a NEW offer (a different
        `offer_tag`) the result becomes eligible again — otherwise the local
        cap would silently retire results the Worker is still asking about,
        which is the same defect as marking a validator_error done.
        """
        state = self.state_of(result_id)
        if state == STATE_SUBMITTED:
            return True
        if state == STATE_FAILED:
            return offer_tag is None or self.offer_tag_of(result_id) == offer_tag
        return False

    # ── transitions (each one commits immediately) ──────────────────────────
    def claim(self, result_id: int, molecule_hash: str = "",
              offer_tag: str = "") -> int:
        """Mark the item as being worked on; returns the new attempt count.

        Committed BEFORE any scoring work, so a crash mid-score is visible as
        a claimed row with an incremented attempt count.

        `offer_tag` identifies the Worker's offer.  When it differs from the
        stored tag this is a NEW offer, so the local attempt budget resets to
        1 instead of accumulating across offers (a result that soft-errors on
        three separate offers must still be re-scored on the fourth).
        """
        row = self._db.execute(
            "SELECT offer_tag FROM progress WHERE result_id=?", (result_id,)
        ).fetchone()
        new_offer = row is None or (row[0] or "") != offer_tag
        if new_offer:
            self._db.execute(
                "INSERT INTO progress(result_id, state, attempts, molecule_hash, "
                "  offer_tag, updated_at) VALUES(?,?,1,?,?,?) "
                "ON CONFLICT(result_id) DO UPDATE SET "
                "  state=excluded.state, attempts=1, "
                "  molecule_hash=excluded.molecule_hash, "
                "  offer_tag=excluded.offer_tag, updated_at=excluded.updated_at",
                (result_id, STATE_CLAIMED, molecule_hash, offer_tag, time.time()),
            )
        else:
            self._db.execute(
                "UPDATE progress SET state=?, attempts=attempts+1, updated_at=? "
                "WHERE result_id=?",
                (STATE_CLAIMED, time.time(), result_id),
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
