"""PayoutSignerService — check a day's batches, then sign or refuse.

Operating rules (all fail-closed):
  * every batch is recomputed from the public log before anything is signed;
  * a day's chunks are checked TOGETHER and signed all-or-nothing;
  * a batchId that was ever signed is never signed again;
  * a coordinator/archive/anchor failure produces NO signature at all;
  * every decision — sign or refuse — is appended to the ledger with the
    exact differences that produced it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import constants as C
from .batch_hash import batch_hash_bytes, split_batch_id
from .errors import (
    COORDINATOR_UNREACHABLE,
    CoordinatorUnreachable,
    Finding,
)
from .ledger import DecisionLedger
from .recompute import build_submitted_at_index, compute_day, DayComputation
from .signer_key import PayoutSigner
from .sources import AnchorSource, LogSource, OperatorInputs
from .summary import summarize_day, summary_record
from .verifier import Batch, verify_day, VerifyResult


@dataclass
class DayDecision:
    day: int
    signed: bool
    results: list[VerifyResult] = field(default_factory=list)
    signatures: dict[int, str] = field(default_factory=dict)
    summary: str = ""
    error: str | None = None

    @property
    def refused(self) -> bool:
        return not self.signed

    def reasons(self) -> list[str]:
        if self.error:
            return [self.error]
        return [r for res in self.results for r in res.reasons()]


class PayoutSignerService:
    def __init__(self, log_source: LogSource, anchors: AnchorSource,
                 operator: OperatorInputs, ledger: DecisionLedger,
                 signer: PayoutSigner | None = None):
        self.log = log_source
        self.anchors = anchors
        self.operator = operator
        self.ledger = ledger
        self.signer = signer

    # ── the one entry point ─────────────────────────────────────────────────
    def process_day(self, day: int, batches: Sequence[dict[str, Any]],
                    now_ms: int | None = None,
                    prior_hit_counts: dict[str, int] | None = None) -> DayDecision:
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)

        # Parse first: a malformed batch file is a refusal, not a crash.
        parsed: list[Batch] = []
        for raw in batches:
            try:
                parsed.append(Batch.from_json(raw))
            except ValueError as exc:
                return self._refuse_hard(day, f"malformed batch file: {exc}")

        # Fail closed on any source failure — no signature, no partial work.
        try:
            day_log = self.log.day_log(day)
            feed_rows = self.log.feed()
            anchored = self.anchors.anchored_hash(day)
        except CoordinatorUnreachable as exc:
            return self._refuse_hard(
                day, f"{COORDINATOR_UNREACHABLE}: {exc}; producing no signature")

        submitted_at = build_submitted_at_index(feed_rows)
        computation = compute_day(
            day_log=day_log,
            submitted_at=submitted_at,
            now_ms=now_ms,
            operator=self.operator,
            already_paid_result_ids=self.ledger.paid_result_ids,
            prior_hit_counts=prior_hit_counts,
        )

        results = verify_day(
            batches=parsed,
            computation=computation,
            day_log=day_log,
            anchored_log_hash=anchored,
            operator=self.operator,
            signed_batch_ids=self.ledger.signed_batch_ids,
            paid_result_ids=self.ledger.paid_result_ids,
        )

        previous = self.ledger.last_signed_day_summary(day)
        summary = summarize_day(day, results, computation.entries, previous)

        all_ok = bool(results) and all(r.signable for r in results)
        signatures: dict[int, str] = {}

        if all_ok and self.signer is None:
            all_ok = False
            for r in results:
                r.signable = False
                r.findings.append(Finding(
                    "NO_PAYOUT_KEY",
                    "verification passed but no payout key is loaded; "
                    "producing no signature"))

        if all_ok:
            # Sign every chunk only after the WHOLE day has passed.
            assert self.signer is not None
            for r, b in zip(results, parsed):
                signatures[b.batch_id] = self.signer.sign_batch_hash(
                    batch_hash_bytes(b.batch_id, b.day, b.total_raw))

        rec = summary_record(day, computation.entries, results)
        for r, b in zip(results, parsed):
            entry_ids = [e.result_id for e in r.expected_entries]
            self.ledger.record_decision({
                "verdict": "sign" if (all_ok and r.signable) else "refuse",
                "day": day,
                "batchId": str(b.batch_id),
                "seq": r.seq,
                "totalRaw": b.total_raw,
                "entryCount": len(b.entries),
                "walletCount": rec["walletCount"],
                "recipients": rec["recipients"],
                "batchHash": r.batch_hash_recomputed,
                "findings": [f.as_dict() for f in r.findings],
                "warnings": [w.as_dict() for w in r.warnings],
                "signature": signatures.get(b.batch_id),
                "payoutPubkey": self.signer.pubkey if self.signer else None,
            })
            if all_ok and r.signable:
                self.ledger.mark_signed(b.batch_id, entry_ids)

        return DayDecision(day=day, signed=all_ok, results=list(results),
                           signatures=signatures, summary=summary)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _refuse_hard(self, day: int, message: str) -> DayDecision:
        """A refusal that never reached verification.  No signature, logged."""
        self.ledger.record_decision({
            "verdict": "refuse",
            "day": day,
            "batchId": None,
            "totalRaw": 0,
            "entryCount": 0,
            "findings": [{"code": COORDINATOR_UNREACHABLE, "message": message}],
            "signature": None,
        })
        return DayDecision(day=day, signed=False, results=[], signatures={},
                           summary=(f"PAYOUT REVIEW — day {day}\n"
                                    f"Decision: REFUSED — DO NOT SIGN\n\n{message}"),
                           error=message)
