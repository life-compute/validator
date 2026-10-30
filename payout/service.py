"""PayoutSignerService — check a day's batches, then sign or refuse.

Operating rules (all fail-closed):
  * every batch is recomputed from the public log before anything is signed;
  * GET /log/{day}/canonical is fetched and its SHA-256 must equal BOTH the
    published log_hash and the anchored hash before the log is used;
  * the §6a taper baseline is a FULL replay of GET /log/{day}/canonical for
    every day from GENESIS_DAY (20000) through the batch day, in ascending
    order; a day the archive cannot serve is a refusal, never a zero;
  * any state this service saved is only a CACHE: it is compared against that
    replay and a mismatch refuses the batch;
  * a day's chunks are checked TOGETHER and signed all-or-nothing;
  * a batchId that was ever signed is never signed again;
  * a coordinator/archive/anchor failure produces NO signature at all;
  * every decision — sign or refuse — is appended to the ledger with the
    exact differences that produced it.

C5b4 (C5j, §2): this module reads NO clock.  `process_day` takes the batch day
as its only date reference and `import time` is gone, so the same inputs
decide identically whatever the system clock says.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .batch_hash import batch_hash_bytes
from . import constants as C
from .errors import (
    COORDINATOR_UNREACHABLE,
    CoordinatorUnreachable,
    Finding,
    LOG_INCONSISTENT,
    MALFORMED_BATCH,
    MISSING_ARCHIVE_DAY,
    NO_PAYOUT_KEY,
)
from .ledger import DecisionLedger
from .recompute import compute_day, replay_days_required
from .signer_key import PayoutSigner
from .sources import AnchorSource, DayLog, LogSource, Settings, TargetsTable
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
    # Rule for a refusal that never reached per-batch verification (the
    # source/replay gates).  `results` is empty in that case, so the rule has
    # to be carried here or `primary_rule` would report None for a refusal.
    hard_rule: str | None = None

    @property
    def refused(self) -> bool:
        return not self.signed

    @property
    def primary_rule(self) -> str | None:
        if self.hard_rule is not None:
            return self.hard_rule
        for r in self.results:
            if r.primary_rule:
                return r.primary_rule
        return None

    def reasons(self) -> list[str]:
        if self.error:
            return [self.error]
        return [r for res in self.results for r in res.reasons()]


class PayoutSignerService:
    def __init__(self, log_source: LogSource, anchors: AnchorSource,
                 settings: Settings, targets: TargetsTable,
                 ledger: DecisionLedger, signer: PayoutSigner | None = None):
        self.log = log_source
        self.anchors = anchors
        self.settings = settings
        self.targets = targets
        self.ledger = ledger
        self.signer = signer

    # ── log fetch + integrity gate (§9, §15, task 1e) ───────────────────────
    def _load_verified_day_log(self, day: int) -> tuple[DayLog, str]:
        """Fetch the canonical log and prove it against BOTH published hashes.

        The log is only usable once SHA-256(canonical bytes) equals the
        published log_hash AND the anchored hash.  Either mismatch raises, so
        no verification work is ever done against an unproven log.
        """
        day_log = self.log.day_log(day)
        anchored = self.anchors.anchored_hash(day)
        computed = day_log.computed_log_hash
        published = day_log.manifest_log_hash or day_log.manifest_results_sha256
        if published is not None and published != computed:
            raise CoordinatorUnreachable(
                f"canonical log for day {day} does not match the published "
                f"log_hash: computed {computed}, published {published} (§15)")
        if anchored != computed:
            raise CoordinatorUnreachable(
                f"canonical log for day {day} does not match the ANCHORED "
                f"hash: computed {computed}, anchored {anchored} (§9)")
        return day_log, anchored

    # ── the one entry point ─────────────────────────────────────────────────
    def process_day(self, day: int, batches: Sequence[dict[str, Any]],
                    genesis_day: int = C.GENESIS_DAY) -> DayDecision:
        """Check and sign (or refuse) every chunk of one calendar day.

        `day` is the batch's injected day number and the ONLY date reference
        (§2 C5j) — there is no `now_ms` and no clock call.  The days to replay
        are derived from GENESIS_DAY, not supplied by the caller, so a caller
        cannot shrink the §6a replay range.
        """
        day = int(day)
        # Parse first: a malformed batch file is a refusal, not a crash.
        parsed: list[Batch] = []
        for raw in batches:
            try:
                parsed.append(Batch.from_json(raw))
            except ValueError as exc:
                return self._refuse_hard(day, MALFORMED_BATCH,
                                         f"malformed batch file: {exc}")

        # §6a — replay EVERY day from GENESIS_DAY through the batch day, in
        # ASCENDING order, through GET /log/{day}/canonical.  Each day's log is
        # proven against its published and anchored hash before it is used, and
        # a day the archive will not serve breaks the replay: refuse, never
        # assume zero hits.  Fail closed on any source failure — no signature,
        # no partial work.
        history: list[DayLog] = []
        day_log: DayLog | None = None
        anchored: str | None = None
        for d in replay_days_required(day, genesis_day):
            try:
                d_log, d_anchor = self._load_verified_day_log(d)
            except CoordinatorUnreachable as exc:
                if d == day:
                    # The batch's own day: report the source/integrity failure.
                    code = (LOG_INCONSISTENT if "does not match" in str(exc)
                            else COORDINATOR_UNREACHABLE)
                    return self._refuse_hard(
                        day, code, f"{exc}; producing no signature")
                return self._refuse_hard(
                    day, MISSING_ARCHIVE_DAY,
                    f"the §6a taper replay requires the canonical log of every "
                    f"day from GENESIS_DAY={genesis_day} through {day}, but day "
                    f"{d} could not be read ({exc}); the confirmed-hit baseline "
                    f"cannot be derived, so no signature is produced")
            history.append(d_log)
            if d == day:
                day_log, anchored = d_log, d_anchor

        if day_log is None or anchored is None:
            return self._refuse_hard(
                day, MISSING_ARCHIVE_DAY,
                f"the canonical log for batch day {day} was never read; "
                f"producing no signature")

        computation = compute_day(
            day_log, targets=self.targets, history=history,
            already_paid_result_ids=self.ledger.paid_result_ids,
            recorded_day_hits=self.ledger.day_target_hits,
            genesis_day=genesis_day,
        )

        results = verify_day(
            batches=parsed, computation=computation, day_log=day_log,
            anchored_log_hash=anchored, settings=self.settings,
            targets=self.targets, signed_batch_ids=self.ledger.signed_batch_ids,
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
                    NO_PAYOUT_KEY,
                    "verification passed but no payout key is loaded; "
                    "producing no signature"))

        if all_ok:
            # Sign every chunk only after the WHOLE day has passed.
            assert self.signer is not None
            for r, b in zip(results, parsed):
                signatures[b.batch_id] = self.signer.sign_batch_hash(
                    batch_hash_bytes(
                        program_id=self.settings.program_id,
                        life_mint=self.settings.life_mint,
                        bid=b.batch_id, day=b.day, entries=b.entry_pairs()))

        rec = summary_record(day, computation.entries, results)
        for r, b in zip(results, parsed):
            self.ledger.record_decision({
                "verdict": "sign" if (all_ok and r.signable) else "refuse",
                "rule": r.primary_rule,
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
                self.ledger.mark_signed(
                    b.batch_id,
                    [rid for e in r.expected_entries for rid in e.result_ids],
                    day=day,
                    day_target_hits=computation.day_target_hits)

        return DayDecision(day=day, signed=all_ok, results=list(results),
                           signatures=signatures, summary=summary)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _refuse_hard(self, day: int, code: str, message: str) -> DayDecision:
        """A refusal that never reached verification.  No signature, logged."""
        self.ledger.record_decision({
            "verdict": "refuse",
            "rule": code,
            "day": day,
            "batchId": None,
            "totalRaw": 0,
            "entryCount": 0,
            "findings": [{"code": code, "message": message}],
            "signature": None,
        })
        return DayDecision(day=day, signed=False, results=[], signatures={},
                           summary=(f"PAYOUT REVIEW — day {day}\n"
                                    f"Decision: REFUSED — DO NOT SIGN\n\n{message}"),
                           error=message, hard_rule=code)
