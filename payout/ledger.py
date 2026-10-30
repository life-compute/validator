"""Append-only decision ledger: every sign/refuse decision, with differences.

Two jobs:
  * durable replay guard — a batchId that was ever signed is never signed
    again, and a result_id that was ever paid is never paid again;
  * audit trail — every decision is logged with the findings that produced it,
    including refusals.

It also keeps the §6.2 taper baseline: for every day this checker signed, the
per-target confirmed-hit counts IT derived from that day's canonical log.  The
next day's taper is computed from those recorded counts plus the earlier days'
logs, so the hit count never comes from the coordinator.

Storage is a JSONL file plus a small JSON index, both under the service's
state directory.  No secret material is ever written: signatures are public,
keys are not stored.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator


class DecisionLedger:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.state_dir / "payout_decisions.jsonl"
        self.index_path = self.state_dir / "payout_index.json"
        self._index = self._load_index()

    # ── index ───────────────────────────────────────────────────────────────
    def _load_index(self) -> dict[str, Any]:
        if self.index_path.is_file():
            try:
                obj = json.loads(self.index_path.read_text())
                if isinstance(obj, dict):
                    obj.setdefault("signed_batch_ids", [])
                    obj.setdefault("paid_result_ids", [])
                    obj.setdefault("day_target_hits", {})
                    return obj
            except json.JSONDecodeError:
                pass
        return {"signed_batch_ids": [], "paid_result_ids": [],
                "day_target_hits": {}}

    def _save_index(self) -> None:
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=self.state_dir, delete=False, suffix=".tmp")
        try:
            json.dump(self._index, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.index_path)
        except Exception:
            tmp.close()
            Path(tmp.name).unlink(missing_ok=True)
            raise

    @property
    def signed_batch_ids(self) -> set[int]:
        return {int(x) for x in self._index["signed_batch_ids"]}

    @property
    def paid_result_ids(self) -> set[int]:
        return {int(x) for x in self._index["paid_result_ids"]}

    def was_signed(self, batch_id: int) -> bool:
        return int(batch_id) in self.signed_batch_ids

    # ── §6.2 taper baseline ─────────────────────────────────────────────────
    @property
    def day_target_hits(self) -> dict[int, dict[str, int]]:
        """day -> {target: confirmed hits}, as THIS checker counted them."""
        return {int(d): {str(t): int(n) for t, n in counts.items()}
                for d, counts in self._index.get("day_target_hits", {}).items()}

    def target_hits_before(self, day: int) -> dict[str, int]:
        """Confirmed hits per target recorded for all days before *day*."""
        out: dict[str, int] = {}
        for d, counts in self.day_target_hits.items():
            if d >= int(day):
                continue
            for tgt, n in counts.items():
                out[tgt] = out.get(tgt, 0) + n
        return out

    # ── writes ──────────────────────────────────────────────────────────────
    def record_decision(self, record: dict[str, Any]) -> dict[str, Any]:
        entry = dict(record)
        entry.setdefault("ts", int(time.time() * 1000))
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    def mark_signed(self, batch_id: int, result_ids: list[int],
                    day: int | None = None,
                    day_target_hits: dict[str, int] | None = None) -> None:
        signed = self.signed_batch_ids
        bid = int(batch_id)
        if bid in signed:
            raise ValueError(
                f"batchId {bid} is already in the ledger — refusing to sign twice")
        self._index["signed_batch_ids"] = sorted(signed | {bid})
        self._index["paid_result_ids"] = sorted(
            self.paid_result_ids | {int(r) for r in result_ids})
        # Record the day's own confirmed-hit counts so the next day's taper
        # baseline survives a restart without re-reading every earlier log.
        if day is not None and day_target_hits is not None:
            self._index.setdefault("day_target_hits", {})[str(int(day))] = {
                str(t): int(n) for t, n in day_target_hits.items()}
        self._save_index()

    # ── reads ───────────────────────────────────────────────────────────────
    def decisions(self) -> Iterator[dict[str, Any]]:
        if not self.log_path.is_file():
            return
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)

    def signed_totals_by_day(self) -> dict[int, int]:
        """day -> total raw already signed, for CAP_PER_DAY across runs."""
        out: dict[int, int] = {}
        for d in self.decisions():
            if d.get("verdict") == "sign":
                day = int(d.get("day", -1))
                out[day] = out.get(day, 0) + int(d.get("totalRaw", 0))
        return out

    def last_signed_day_summary(self, before_day: int) -> dict[str, Any] | None:
        """Most recent signed day strictly before *before_day* (for diffing)."""
        best: dict[str, Any] | None = None
        for d in self.decisions():
            if d.get("verdict") != "sign":
                continue
            day = int(d.get("day", -1))
            if day < int(before_day) and (best is None or day > int(best["day"])):
                best = d
        return best
