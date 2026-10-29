"""Verify a coordinator-supplied batch against an independent recomputation.

The verifier collects EVERY finding it can rather than short-circuiting, so
the decision log shows the operator all the differences at once.  A batch is
signable only when the finding list is empty.

Checks, in order:
  1. structural    — field types, bigint-as-string, no duplicate result ids
  2. identity      — batchId == day*1000+seq, recomputed batchHash matches
  3. log integrity — computed logHash == batch.logHash == manifest == anchor
  4. entry-by-entry— the recomputed entry set, order, recipients, amounts
  5. caps          — MAX_BATCH_ENTRIES, CAP_PER_BATCH, CAP_PER_DAY, miner cap
  6. replay        — batchId never signed before; result paid at most once
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import constants as C
from .batch_hash import batch_hash_hex, batch_id as compute_batch_id, split_batch_id
from .errors import (
    ANCHOR_HASH_MISMATCH,
    BATCH_HASH_MISMATCH,
    BATCH_ID_ALREADY_SIGNED,
    BATCH_ID_MISMATCH,
    BATCH_TOTAL_ABOVE_CAP,
    CHUNK_SEQUENCE_INVALID,
    DAILY_MINER_CAP_EXCEEDED,
    DAY_TOTAL_ABOVE_CAP,
    DUPLICATE_RESULT_ACROSS_CHUNKS,
    DUPLICATE_RESULT_IN_BATCH,
    ENTRY_AMOUNT_MISMATCH,
    ENTRY_COUNT_ABOVE_LIMIT,
    ENTRY_FIELD_MISMATCH,
    ENTRY_ORDER_MISMATCH,
    ENTRY_RECIPIENT_MISMATCH,
    ENTRY_SET_MISMATCH,
    Finding,
    LOG_HASH_MISMATCH,
    LOG_INCONSISTENT,
    NO_CONFIRMED_RESULT,
    RESULT_ALREADY_PAID,
    TOTAL_NOT_SUM_OF_ENTRIES,
    VALIDATOR_FEE_MISMATCH,
    WALLET_FLAGGED,
    WARN_FEE_RECIPIENTS_UNVERIFIABLE,
    WARN_SPEC_CONFLICT,
)
from .recompute import ComputedEntry, DayComputation
from .sources import DayLog, OperatorInputs


@dataclass
class Batch:
    """A coordinator batch file (BATCH_SPEC §7), parsed defensively."""
    batch_id: int
    day: int
    total_raw: int
    batch_hash: str
    entries: list[dict[str, Any]]
    log_hash: str | None
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Batch":
        def _bigint(name: str) -> int:
            v = obj.get(name)
            if isinstance(v, bool) or not isinstance(v, (str, int)):
                raise ValueError(f"batch field {name!r} must be a decimal string, got {v!r}")
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"batch field {name!r} is not an integer: {v!r}") from None
            if n < 0:
                raise ValueError(f"batch field {name!r} is negative: {n}")
            return n

        entries = obj.get("entries")
        if not isinstance(entries, list):
            raise ValueError("batch field 'entries' must be a list")
        day = obj.get("day")
        if isinstance(day, bool) or not isinstance(day, int):
            raise ValueError(f"batch field 'day' must be a u32 integer, got {day!r}")
        bh = obj.get("batchHash")
        if not isinstance(bh, str) or len(bh) != 64:
            raise ValueError("batch field 'batchHash' must be 64 hex chars")
        lh = obj.get("logHash")
        return cls(batch_id=_bigint("batchId"), day=day, total_raw=_bigint("totalRaw"),
                   batch_hash=bh.lower(), entries=entries,
                   log_hash=lh.lower() if isinstance(lh, str) else None, raw=obj)


@dataclass
class VerifyResult:
    batch_id: int
    day: int
    seq: int
    signable: bool
    findings: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    expected_entries: list[ComputedEntry] = field(default_factory=list)
    batch_hash_recomputed: str | None = None
    total_raw: int = 0

    @property
    def verdict(self) -> str:
        return "sign" if self.signable else "refuse"

    def reasons(self) -> list[str]:
        return [f"{f.code}: {f.message}" for f in self.findings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "batchId": str(self.batch_id),
            "day": self.day,
            "seq": self.seq,
            "totalRaw": str(self.total_raw),
            "entryCount": len(self.expected_entries),
            "batchHash": self.batch_hash_recomputed,
            "findings": [f.as_dict() for f in self.findings],
            "warnings": [w.as_dict() for w in self.warnings],
        }


def _entry_int(entry: dict[str, Any], key: str) -> int | None:
    v = entry.get(key)
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def verify_batch(
    batch: Batch,
    computation: DayComputation,
    day_log: DayLog,
    anchored_log_hash: str,
    operator: OperatorInputs,
    signed_batch_ids: set[int],
    paid_result_ids: set[int] | None = None,
    day_total_other_chunks: int = 0,
    expected_chunk: Sequence[ComputedEntry] | None = None,
) -> VerifyResult:
    """Full per-batch verification.  Returns a VerifyResult; never raises."""
    findings: list[Finding] = list(computation.findings)
    warnings: list[Finding] = [Finding(WARN_SPEC_CONFLICT, c) for c in C.SPEC_CONFLICTS]
    paid_result_ids = set(paid_result_ids or ())
    day, seq = split_batch_id(batch.batch_id)

    # ── 2. identity ─────────────────────────────────────────────────────────
    if day != batch.day:
        findings.append(Finding(
            BATCH_ID_MISMATCH,
            f"batchId {batch.batch_id} decodes to day {day}, but the batch "
            f"declares day {batch.day} (BATCH_SPEC §3)"))
    expected_id = compute_batch_id(batch.day, seq)
    if batch.batch_id != expected_id:
        findings.append(Finding(
            BATCH_ID_MISMATCH,
            f"batchId {batch.batch_id} != day*1000+seq = {expected_id} (§3)"))

    recomputed_hash = batch_hash_hex(batch.batch_id, batch.day, batch.total_raw)
    if recomputed_hash != batch.batch_hash:
        findings.append(Finding(
            BATCH_HASH_MISMATCH,
            f"batchHash mismatch: recomputed {recomputed_hash} from "
            f"(batchId={batch.batch_id}, day={batch.day}, totalRaw={batch.total_raw}), "
            f"batch claims {batch.batch_hash} (§4)"))

    # ── 6a. replay on batch id ──────────────────────────────────────────────
    if batch.batch_id in signed_batch_ids:
        findings.append(Finding(
            BATCH_ID_ALREADY_SIGNED,
            f"batchId {batch.batch_id} has already been signed by this service "
            f"(§10 replay protection); refusing to sign it twice"))

    # ── 3. log integrity ────────────────────────────────────────────────────
    computed_log_hash = day_log.computed_log_hash
    if batch.log_hash is not None and batch.log_hash != computed_log_hash:
        findings.append(Finding(
            LOG_HASH_MISMATCH,
            f"batch.logHash {batch.log_hash} does not match SHA-256 of the "
            f"canonical daily log (computed {computed_log_hash}) (§9)"))
    manifest_hash = day_log.manifest_results_sha256
    if manifest_hash and manifest_hash.lower() != computed_log_hash:
        findings.append(Finding(
            LOG_INCONSISTENT,
            f"GET /log/{day_log.day} manifest results.sha256 {manifest_hash} "
            f"does not match the canonical log body (computed {computed_log_hash})"))
    if anchored_log_hash.lower() != computed_log_hash:
        findings.append(Finding(
            ANCHOR_HASH_MISMATCH,
            f"anchored log hash {anchored_log_hash} for day {day_log.day} does "
            f"not match the canonical log (computed {computed_log_hash}) (§9)"))

    # ── 1. structural ───────────────────────────────────────────────────────
    log_by_id = {r.result_id: r for r in day_log.records}
    expected = list(expected_chunk if expected_chunk is not None else computation.entries)
    expected_by_id = {e.result_id: e for e in expected}

    seen_ids: set[int] = set()
    entry_sum = 0
    per_wallet: dict[str, int] = {}

    for idx, ent in enumerate(batch.entries):
        rid = _entry_int(ent, "resultId")
        if rid is None:
            findings.append(Finding(
                ENTRY_FIELD_MISMATCH,
                f"entry #{idx}: resultId is missing or not an integer: {ent.get('resultId')!r}"))
            continue
        if rid in seen_ids:
            findings.append(Finding(
                DUPLICATE_RESULT_IN_BATCH,
                f"result_id {rid} appears more than once in batch {batch.batch_id}", rid))
            continue
        seen_ids.add(rid)

        amount = _entry_int(ent, "amountRaw")
        if amount is None or amount < 0:
            findings.append(Finding(
                ENTRY_FIELD_MISMATCH,
                f"entry resultId {rid}: amountRaw must be a non-negative decimal "
                f"string, got {ent.get('amountRaw')!r}", rid))
            amount = 0
        entry_sum += amount
        recipient = ent.get("recipient")

        # 6b. already paid
        if rid in paid_result_ids:
            findings.append(Finding(
                RESULT_ALREADY_PAID,
                f"result_id {rid} is already recorded as paid; paying it again "
                f"would double-pay (§5.1.6, §10)", rid))

        # 4a. must exist as a confirmed result in the canonical log
        if rid not in log_by_id:
            findings.append(Finding(
                NO_CONFIRMED_RESULT,
                f"result_id {rid} has no confirmed verdict "
                f"(coord_decision=confirm + hit_status=confirmed_hit) in the "
                f"canonical log for day {day_log.day}", rid))
            continue

        rec = log_by_id[rid]
        if isinstance(recipient, str) and recipient != rec.wallet:
            findings.append(Finding(
                ENTRY_RECIPIENT_MISMATCH,
                f"result_id {rid}: batch pays {recipient} but the canonical log "
                f"records wallet {rec.wallet}", rid))
        if recipient in operator.flagged_wallets:
            findings.append(Finding(
                WALLET_FLAGGED,
                f"recipient {str(recipient)[:8]}… appears in wallet_flags "
                f"(flagged=1) — ineligible for payout (§5.1.4)", rid))
        if ent.get("target") not in (None, rec.target):
            findings.append(Finding(
                ENTRY_FIELD_MISMATCH,
                f"result_id {rid}: batch target {ent.get('target')!r} != log "
                f"target {rec.target!r}", rid))
        if ent.get("moleculeHash") not in (None, rec.molecule_hash):
            findings.append(Finding(
                ENTRY_FIELD_MISMATCH,
                f"result_id {rid}: batch moleculeHash does not match the "
                f"canonical log record", rid))

        # 4b. amount must equal the independently computed reward
        exp = expected_by_id.get(rid)
        if exp is None:
            reason = computation.skipped.get(rid, "not payable on this day per §5.1")
            findings.append(Finding(
                ENTRY_SET_MISMATCH,
                f"result_id {rid} is in the batch but the independent "
                f"recomputation does not pay it: {reason}", rid))
        else:
            if amount != exp.amount_raw:
                verb = "exceeds" if amount > exp.amount_raw else "is below"
                findings.append(Finding(
                    ENTRY_AMOUNT_MISMATCH,
                    f"entry resultId {rid}: amountRaw {verb} computed reward — "
                    f"expected {exp.amount_raw} "
                    f"(base {exp.base_reward} x {exp.taper_num}/4, "
                    f"hitCountBefore={exp.hit_count_before}), got {amount}", rid))
            fees = ent.get("validatorFees")
            if isinstance(fees, list):
                for fee in fees:
                    fv = _entry_int(fee, "amountRaw") if isinstance(fee, dict) else None
                    if fv is not None and fv != exp.validator_fee_raw:
                        findings.append(Finding(
                            VALIDATOR_FEE_MISMATCH,
                            f"result_id {rid}: validator fee {fv} != "
                            f"floor({exp.amount_raw} * {C.VALIDATOR_FEE_BPS} / "
                            f"{C.BPS_DENOM}) = {exp.validator_fee_raw} (§6.3)", rid))

        per_wallet[rec.wallet] = per_wallet.get(rec.wallet, 0) + 1

    # 4c. entries the recomputation expects but the batch omits
    for rid in expected_by_id:
        if rid not in seen_ids:
            e = expected_by_id[rid]
            findings.append(Finding(
                ENTRY_SET_MISMATCH,
                f"result_id {rid} ({e.recipient[:8]}…, {e.amount_raw} raw) is "
                f"payable per the public log but missing from the batch", rid))

    # 4d. ordering (§5.5)
    if expected_chunk is not None and not findings:
        batch_order = [_entry_int(e, "resultId") for e in batch.entries]
        if batch_order != [e.result_id for e in expected]:
            findings.append(Finding(
                ENTRY_ORDER_MISMATCH,
                f"entry order {batch_order} does not match the spec order "
                f"(archived_at asc, then result_id asc): "
                f"{[e.result_id for e in expected]} (§5.5)"))

    # ── 5. caps ─────────────────────────────────────────────────────────────
    if len(batch.entries) > C.MAX_BATCH_ENTRIES:
        findings.append(Finding(
            ENTRY_COUNT_ABOVE_LIMIT,
            f"entry count {len(batch.entries)} exceeds "
            f"MAX_BATCH_ENTRIES={C.MAX_BATCH_ENTRIES} (§5.4, §5.6)"))

    if batch.total_raw != entry_sum:
        findings.append(Finding(
            TOTAL_NOT_SUM_OF_ENTRIES,
            f"totalRaw {batch.total_raw} != sum of miner entry amounts "
            f"{entry_sum}; validator fees are excluded from totalRaw (§4, §6.3)"))

    if batch.total_raw > C.CAP_PER_BATCH:
        findings.append(Finding(
            BATCH_TOTAL_ABOVE_CAP,
            f"batch totalRaw {batch.total_raw} exceeds "
            f"CAP_PER_BATCH={C.CAP_PER_BATCH} (5,000 LIFE) (§5.3)"))

    day_total = batch.total_raw + day_total_other_chunks
    if day_total > C.CAP_PER_DAY:
        findings.append(Finding(
            DAY_TOTAL_ABOVE_CAP,
            f"day total {day_total} would exceed CAP_PER_DAY={C.CAP_PER_DAY} "
            f"(5,000 LIFE cumulative over all chunks) (§5.3)"))

    for wallet, n in per_wallet.items():
        if n > C.DAILY_MINER_CAP:
            findings.append(Finding(
                DAILY_MINER_CAP_EXCEEDED,
                f"wallet {wallet[:8]}… is paid {n} results in this batch, above "
                f"DAILY_MINER_CAP={C.DAILY_MINER_CAP} (§5.2)"))

    if seq > 0 and expected_chunk is None:
        findings.append(Finding(
            CHUNK_SEQUENCE_INVALID,
            f"batch declares seq={seq} but no chunk plan was supplied; a "
            f"non-zero chunk can only be verified as part of a whole-day run (§5.4)"))

    any_fee_unverifiable = any(
        isinstance(e.get("validatorFees"), list) and
        any(isinstance(f, dict) and "validatorWallet" in f for f in e["validatorFees"])
        for e in batch.entries)
    if any_fee_unverifiable:
        warnings.append(Finding(
            WARN_FEE_RECIPIENTS_UNVERIFIABLE,
            "validator fee RECIPIENTS cannot be verified from the public log: "
            "the canonical log line (§9) carries no validator_wallet field. "
            "Fee AMOUNTS are checked against §6.3; the payee list is not."))

    return VerifyResult(
        batch_id=batch.batch_id, day=batch.day, seq=seq,
        signable=not findings, findings=findings, warnings=warnings,
        expected_entries=expected, batch_hash_recomputed=recomputed_hash,
        total_raw=batch.total_raw,
    )


def verify_day(
    batches: Sequence[Batch],
    computation: DayComputation,
    day_log: DayLog,
    anchored_log_hash: str,
    operator: OperatorInputs,
    signed_batch_ids: set[int],
    paid_result_ids: set[int] | None = None,
) -> list[VerifyResult]:
    """Verify ALL of a day's chunks together (§5.4).

    No chunk is signable unless every chunk of the day passes: the chunks
    together must stay inside CAP_PER_DAY and no result may be paid twice
    across chunks.
    """
    results: list[VerifyResult] = []
    chunks = computation.chunks()
    day_total = sum(b.total_raw for b in batches)

    cross: dict[int, list[int]] = {}
    for b in batches:
        for ent in b.entries:
            rid = _entry_int(ent, "resultId")
            if rid is not None:
                cross.setdefault(rid, []).append(b.batch_id)

    dupes = {rid: ids for rid, ids in cross.items() if len(ids) > 1}

    for b in batches:
        _, seq = split_batch_id(b.batch_id)
        expected_chunk = chunks[seq] if seq < len(chunks) else []
        res = verify_batch(
            b, computation, day_log, anchored_log_hash, operator,
            signed_batch_ids, paid_result_ids,
            day_total_other_chunks=day_total - b.total_raw,
            expected_chunk=expected_chunk,
        )
        if seq >= len(chunks):
            res.findings.append(Finding(
                CHUNK_SEQUENCE_INVALID,
                f"batch seq={seq} has no counterpart in the recomputed chunk "
                f"plan ({len(chunks)} chunk(s) expected for day {day_log.day}) (§5.4)"))
        for rid, ids in dupes.items():
            if any(bid == b.batch_id for bid in ids):
                res.findings.append(Finding(
                    DUPLICATE_RESULT_ACROSS_CHUNKS,
                    f"result_id {rid} is paid in multiple chunks of day "
                    f"{day_log.day}: batchIds {sorted(set(ids))} (§5.4, §10)", rid))
        res.signable = not res.findings
        results.append(res)

    # Fail the whole day together: one bad chunk blocks every chunk.
    if any(not r.signable for r in results):
        for r in results:
            if r.signable:
                r.signable = False
                r.findings.append(Finding(
                    CHUNK_SEQUENCE_INVALID,
                    f"another chunk of day {day_log.day} failed verification; "
                    f"chunks are signed all-or-nothing (§5.4)"))
    return results
