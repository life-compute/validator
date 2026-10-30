"""Verify a coordinator-supplied batch against an independent recomputation.

Rules are evaluated in a FIXED PRECEDENCE and the first rule that fires names
the refusal.  Precedence matters because a tampered batch usually violates
several rules at once: the order below goes from "this batch is not even
addressed to our chain" outward to per-entry arithmetic, so the reported rule
is the most fundamental violation rather than an incidental downstream one.

Precedence (BATCH_SPEC.md section in brackets):
   1 wrong_program_id_or_mint     [§4, §16]  not our program/mint at all
   2 chunk_above_entry_limit      [§5.4/§14] more than 4 wallet entries
   3 batch_total_above_cap        [§5.3]     entry sum over CAP_PER_BATCH
   4 total_above_day_cap          [§5.3]     declared/day total over CAP_PER_DAY
   5 duplicate_recipient          [§4]       same wallet twice (on-chain reject)
   6 sort_contract_violated       [§4]       entries not strictly ascending
   7 inflated_amount              [§7]       entry total != sum(results[])
   8 target_not_in_table          [§12]      target absent from targets table
   9 no_wallet_first_seen         [§13.1]    wallet age unprovable
  10 flagged_wallet_paid          [§13.2]    flagged with no later unflag
  11 forfeited_wallet_paid         [§13.4]    permanent holdback forfeiture
  12 held_wallet_paid              [§5.1.3]   still inside the 7-day holdback

  Forfeiture outranks holdback: a wallet inside its first week that was
  forfeited is BOTH held and forfeited, and forfeiture is the permanent,
  strictly stronger condition (§5.1.5), so it must name the refusal.
  13 no_confirmed_result          [§9]       result_id absent from the log
  14 repeated_batch_id            [§10]      batchId already signed by us
  15 log_hash_mismatch            [§9]       batch.logHash != SHA-256(canonical)
  16 anchor_hash_mismatch         [§9]       anchored hash != SHA-256(canonical)
  17 reward_mismatch              [§6.1/6.2] per-result reward off the rules
  18 validator_fee_mismatch       [§6.3]     fee != floor(reward*750/10000)
  19 everything else (ordering, missing entries, totals, batch hash, replay)

A batch is signable only when NO rule fired.  Every finding is collected for
the decision log; `primary_rule` is the highest-precedence one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import constants as C
from .batch_hash import (
    batch_hash_hex,
    batch_id as compute_batch_id,
    pubkey_bytes,
    split_batch_id,
)
from .errors import (
    ANCHOR_HASH_MISMATCH,
    BATCH_HASH_MISMATCH,
    BATCH_ID_MISMATCH,
    BATCH_TOTAL_ABOVE_CAP,
    CHUNK_ABOVE_ENTRY_LIMIT,
    CHUNK_SEQUENCE_INVALID,
    DAILY_MINER_CAP_EXCEEDED,
    DUPLICATE_RECIPIENT,
    DUPLICATE_RESULT_ACROSS_CHUNKS,
    DUPLICATE_RESULT_IN_BATCH,
    FLAGGED_WALLET_PAID,
    FORFEITED_WALLET_PAID,
    Finding,
    HELD_WALLET_PAID,
    INFLATED_AMOUNT,
    LOG_HASH_MISMATCH,
    LOG_INCONSISTENT,
    MISSING_PAYABLE_ENTRY,
    NO_CONFIRMED_RESULT,
    NO_WALLET_FIRST_SEEN,
    RECIPIENT_NOT_IN_LOG,
    REPEATED_BATCH_ID,
    RESULT_ALREADY_PAID,
    REWARD_MISMATCH,
    SORT_CONTRACT_VIOLATED,
    TARGET_NOT_IN_TABLE,
    TOTAL_ABOVE_DAY_CAP,
    TOTAL_NOT_SUM_OF_ENTRIES,
    VALIDATOR_FEE_MISMATCH,
    WARN_FEE_RECIPIENTS_UNVERIFIABLE,
    WRONG_PROGRAM_ID_OR_MINT,
)
from .recompute import ComputedEntry, DayComputation
from .sources import DayLog, Settings, TargetsTable, WalletFacts

# The precedence order above, as data.  Lower index = reported first.
RULE_PRECEDENCE = (
    WRONG_PROGRAM_ID_OR_MINT,
    CHUNK_ABOVE_ENTRY_LIMIT,
    BATCH_TOTAL_ABOVE_CAP,
    TOTAL_ABOVE_DAY_CAP,
    DUPLICATE_RECIPIENT,
    SORT_CONTRACT_VIOLATED,
    INFLATED_AMOUNT,
    TARGET_NOT_IN_TABLE,
    NO_WALLET_FIRST_SEEN,
    FLAGGED_WALLET_PAID,
    FORFEITED_WALLET_PAID,
    HELD_WALLET_PAID,
    NO_CONFIRMED_RESULT,
    REPEATED_BATCH_ID,
    LOG_HASH_MISMATCH,
    ANCHOR_HASH_MISMATCH,
    REWARD_MISMATCH,
    VALIDATOR_FEE_MISMATCH,
    RECIPIENT_NOT_IN_LOG,
    DUPLICATE_RESULT_IN_BATCH,
    DUPLICATE_RESULT_ACROSS_CHUNKS,
    RESULT_ALREADY_PAID,
    MISSING_PAYABLE_ENTRY,
    DAILY_MINER_CAP_EXCEEDED,
    TOTAL_NOT_SUM_OF_ENTRIES,
    BATCH_HASH_MISMATCH,
    BATCH_ID_MISMATCH,
    CHUNK_SEQUENCE_INVALID,
    LOG_INCONSISTENT,
)
_RULE_RANK = {code: i for i, code in enumerate(RULE_PRECEDENCE)}


def _rank(code: str) -> int:
    return _RULE_RANK.get(code, len(RULE_PRECEDENCE))


@dataclass
class Batch:
    """A coordinator batch file (BATCH_SPEC §7), parsed defensively."""
    batch_id: int
    day: int
    total_raw: int
    batch_hash: str
    entries: list[dict[str, Any]]
    log_hash: str | None
    program_id: str | None
    life_mint: str | None
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
        pid = obj.get("programId")
        mint = obj.get("lifeMint")
        return cls(batch_id=_bigint("batchId"), day=day, total_raw=_bigint("totalRaw"),
                   batch_hash=bh.lower(), entries=entries,
                   log_hash=lh.lower() if isinstance(lh, str) else None,
                   program_id=pid if isinstance(pid, str) else None,
                   life_mint=mint if isinstance(mint, str) else None,
                   raw=obj)

    def entry_pairs(self) -> list[tuple[str, int]]:
        """(recipient, amountRaw) in the batch's own order, for hashing (§4)."""
        out: list[tuple[str, int]] = []
        for ent in self.entries:
            r = ent.get("recipient")
            a = _as_int(ent.get("amountRaw"))
            if isinstance(r, str) and a is not None:
                out.append((r, a))
        return out


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

    @property
    def primary_rule(self) -> str | None:
        """The highest-precedence rule that fired — the named refusal reason."""
        if not self.findings:
            return None
        return min(self.findings, key=lambda f: _rank(f.code)).code

    def reasons(self) -> list[str]:
        ordered = sorted(self.findings, key=lambda f: _rank(f.code))
        return [f"{f.code}: {f.message}" for f in ordered]

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "rule": self.primary_rule,
            "batchId": str(self.batch_id),
            "day": self.day,
            "seq": self.seq,
            "totalRaw": str(self.total_raw),
            "entryCount": len(self.expected_entries),
            "batchHash": self.batch_hash_recomputed,
            "findings": [f.as_dict() for f in
                         sorted(self.findings, key=lambda f: _rank(f.code))],
            "warnings": [w.as_dict() for w in self.warnings],
        }


def _as_int(v: Any) -> int | None:
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
    settings: Settings,
    targets: TargetsTable,
    signed_batch_ids: set[int],
    paid_result_ids: set[int] | None = None,
    day_total_other_chunks: int = 0,
    expected_chunk: Sequence[ComputedEntry] | None = None,
    now_ms: int | None = None,
) -> VerifyResult:
    """Full per-batch verification.  Returns a VerifyResult; never raises."""
    findings: list[Finding] = list(computation.findings)
    warnings: list[Finding] = []
    paid_result_ids = set(paid_result_ids or ())
    day, seq = split_batch_id(batch.batch_id)
    facts = computation.wallet_facts

    # ── 1. program id / mint (§4, §16) ──────────────────────────────────────
    if (batch.program_id or "") != settings.program_id or \
       (batch.life_mint or "") != settings.life_mint:
        findings.append(Finding(
            WRONG_PROGRAM_ID_OR_MINT,
            f"programId or lifeMint does not match required configuration "
            f"(PROGRAM_ID={settings.program_id}, LIFE_MINT={settings.life_mint}); "
            f"batch declares programId={batch.program_id!r}, "
            f"lifeMint={batch.life_mint!r} (§4, §16)"))

    # ── 2. entry-count limit (§5.4, §5.6, §14) ──────────────────────────────
    if len(batch.entries) > C.MAX_BATCH_ENTRIES:
        findings.append(Finding(
            CHUNK_ABOVE_ENTRY_LIMIT,
            f"entry count {len(batch.entries)} exceeds "
            f"MAX_BATCH_ENTRIES={C.MAX_BATCH_ENTRIES} (§5.4, §5.6, §14)"))

    # ── per-entry structural scan ───────────────────────────────────────────
    log_by_id = {r.result_id: r for r in day_log.records}
    expected = list(expected_chunk if expected_chunk is not None else computation.entries)
    expected_by_recipient = {e.recipient: e for e in expected}

    recipients: list[str] = []
    entry_sum = 0
    seen_result_ids: set[int] = set()
    per_wallet_results: dict[str, int] = {}

    for idx, ent in enumerate(batch.entries):
        recipient = ent.get("recipient")
        if not isinstance(recipient, str):
            findings.append(Finding(
                RECIPIENT_NOT_IN_LOG,
                f"entry #{idx}: recipient is missing or not a string"))
            continue
        recipients.append(recipient)

        amount = _as_int(ent.get("amountRaw"))
        if amount is None or amount < 0:
            findings.append(Finding(
                INFLATED_AMOUNT,
                f"entry {recipient[:8]}…: amountRaw must be a non-negative "
                f"decimal string, got {ent.get('amountRaw')!r}", wallet=recipient))
            amount = 0
        entry_sum += amount

        results = ent.get("results")
        results = results if isinstance(results, list) else []

        # §7: every on-chain entry is ONE wallet's total.
        breakdown_sum = sum(_as_int(r.get("amountRaw")) or 0
                            for r in results if isinstance(r, dict))
        if amount != breakdown_sum:
            findings.append(Finding(
                INFLATED_AMOUNT,
                f"entry {recipient[:8]}…: amountRaw {amount} ≠ sum of results "
                f"breakdown {breakdown_sum} — every entry must equal the sum "
                f"of its per-result rewards (§7)", wallet=recipient))

        per_wallet_results[recipient] = per_wallet_results.get(recipient, 0) + len(results)
        exp_entry = expected_by_recipient.get(recipient)
        exp_results = exp_entry.by_result_id() if exp_entry else {}

        for r in results:
            if not isinstance(r, dict):
                continue
            rid = _as_int(r.get("resultId"))
            if rid is None:
                findings.append(Finding(
                    NO_CONFIRMED_RESULT,
                    f"entry {recipient[:8]}…: resultId missing or not an integer",
                    wallet=recipient))
                continue
            if rid in seen_result_ids:
                findings.append(Finding(
                    DUPLICATE_RESULT_IN_BATCH,
                    f"result_id {rid} appears more than once in batch "
                    f"{batch.batch_id}", rid, recipient))
            seen_result_ids.add(rid)

            if rid in paid_result_ids:
                findings.append(Finding(
                    RESULT_ALREADY_PAID,
                    f"result_id {rid} is already recorded as paid; paying it "
                    f"again would double-pay (§5.1.6, §10)", rid, recipient))

            # §12 — target must be in the SHA-verified table.
            target = r.get("target")
            if isinstance(target, str) and targets.get(target) is None:
                findings.append(Finding(
                    TARGET_NOT_IN_TABLE,
                    f"target {target!r} is not present in targets_table.json — "
                    f"the builder must only pay for targets in the table (§12)",
                    rid, recipient))

            # §9 — the result must be a confirmed result in the canonical log.
            rec = log_by_id.get(rid)
            if rec is None:
                findings.append(Finding(
                    NO_CONFIRMED_RESULT,
                    f"result_id {rid} has no confirmed verdict "
                    f"(coord_decision=confirm + hit_status=confirmed_hit) in "
                    f"the canonical log for day {day_log.day} (§9)", rid, recipient))
                continue
            if rec.wallet != recipient:
                findings.append(Finding(
                    RECIPIENT_NOT_IN_LOG,
                    f"result_id {rid}: batch pays {recipient[:8]}… but the "
                    f"canonical log records wallet {rec.wallet[:8]}…", rid, recipient))
            if isinstance(target, str) and target != rec.target:
                findings.append(Finding(
                    TARGET_NOT_IN_TABLE,
                    f"result_id {rid}: batch target {target!r} != canonical log "
                    f"target {rec.target!r}", rid, recipient))

            # §6.1/§6.2 — the reward the spec's rules give it.
            r_amount = _as_int(r.get("amountRaw"))
            exp_r = exp_results.get(rid)
            if exp_r is not None and r_amount is not None and r_amount != exp_r.amount_raw:
                verb = "exceeds" if r_amount > exp_r.amount_raw else "is below"
                findings.append(Finding(
                    REWARD_MISMATCH,
                    f"result_id {rid}: reward {r_amount} {verb} the rules' "
                    f"value {exp_r.amount_raw} = max(1, floor({exp_r.base_reward} "
                    f"× {exp_r.taper_num}/4)) for tier {exp_r.tier!r} with "
                    f"hitCountBefore={exp_r.hit_count_before} (§6.1, §6.2, §12)",
                    rid, recipient))
            elif exp_r is None and rec is not None and recipient in expected_by_recipient:
                reason = computation.skipped.get(rid, "not payable on this day (§5.1)")
                findings.append(Finding(
                    NO_CONFIRMED_RESULT,
                    f"result_id {rid} is in the batch but the independent "
                    f"recomputation does not pay it: {reason}", rid, recipient))

            # §6.3 — validator fees checked the same way.
            fees = r.get("validatorFees")
            if isinstance(fees, list) and exp_r is not None:
                for fee in fees:
                    fv = _as_int(fee.get("amountRaw")) if isinstance(fee, dict) else None
                    if fv is not None and fv != exp_r.validator_fee_raw:
                        findings.append(Finding(
                            VALIDATOR_FEE_MISMATCH,
                            f"result_id {rid}: validator fee {fv} != "
                            f"floor({exp_r.amount_raw} × {C.VALIDATOR_FEE_BPS} / "
                            f"{C.BPS_DENOM}) = {exp_r.validator_fee_raw} (§6.3)",
                            rid, recipient))

        # ── wallet eligibility, from the log's events only (§13) ────────────
        wf = facts.get(recipient)
        if wf is None or wf.first_seen_ms is None:
            findings.append(Finding(
                NO_WALLET_FIRST_SEEN,
                f"recipient {recipient[:8]}… has no wallet_first_seen event in "
                f"the canonical log — wallet age cannot be verified; the "
                f"holdback rule cannot be applied (§13.1, §5.1.3)", wallet=recipient))
        else:
            if wf.flagged:
                findings.append(Finding(
                    FLAGGED_WALLET_PAID,
                    f"recipient {recipient[:8]}… has a wallet_flagged event in "
                    f"the canonical log and no subsequent wallet_unflagged — "
                    f"ineligible for payout (§13.2, §5.1.4)", wallet=recipient))
            if now_ms is not None and (now_ms - wf.first_seen_ms) < C.HOLDBACK_MS:
                findings.append(Finding(
                    HELD_WALLET_PAID,
                    f"recipient {recipient[:8]}… has wallet_first_seen "
                    f"time_ms={wf.first_seen_ms} in the canonical log — wallet "
                    f"age {now_ms - wf.first_seen_ms}ms < "
                    f"HOLDBACK_MS={C.HOLDBACK_MS}ms; first-week result must be "
                    f"held (§5.1.3)", wallet=recipient))
            if wf.forfeited:
                findings.append(Finding(
                    FORFEITED_WALLET_PAID,
                    f"recipient {recipient[:8]}… has a holdback_forfeit event "
                    f"in the canonical log — first-week results are permanently "
                    f"forfeited (§13.4, §5.1.5)", wallet=recipient))

    # ── 5/6. duplicate + sort contract (§4) ─────────────────────────────────
    if len(set(recipients)) != len(recipients):
        dupes = sorted({r for r in recipients if recipients.count(r) > 1})
        findings.append(Finding(
            DUPLICATE_RECIPIENT,
            f"recipient {dupes[0][:8]}… appears twice — on-chain rejects with "
            f"DuplicateRecipient (§4 duplicate contract)"))
    else:
        try:
            keys = [pubkey_bytes(r) for r in recipients]
            if any(keys[i] >= keys[i + 1] for i in range(len(keys) - 1)):
                findings.append(Finding(
                    SORT_CONTRACT_VIOLATED,
                    "entries are not sorted by recipient pubkey bytes ascending "
                    "— violates the §4 sort contract; on-chain rejects as "
                    "DuplicateRecipient/out-of-order"))
        except ValueError as exc:
            findings.append(Finding(
                RECIPIENT_NOT_IN_LOG, f"undecodable recipient public key: {exc}"))

    # ── 3/4. token caps (§5.3) ──────────────────────────────────────────────
    if entry_sum > C.CAP_PER_BATCH:
        findings.append(Finding(
            BATCH_TOTAL_ABOVE_CAP,
            f"batch totalRaw {entry_sum} exceeds "
            f"CAP_PER_BATCH={C.CAP_PER_BATCH} (5,000 LIFE) (§5.3)"))
    day_total = max(batch.total_raw, entry_sum) + day_total_other_chunks
    if day_total > C.CAP_PER_DAY:
        findings.append(Finding(
            TOTAL_ABOVE_DAY_CAP,
            f"day total {day_total} would exceed CAP_PER_DAY={C.CAP_PER_DAY} "
            f"(5,000 LIFE cumulative over all chunks) (§5.3)"))

    # ── 14. replay on batch id (§10) ────────────────────────────────────────
    if batch.batch_id in signed_batch_ids:
        findings.append(Finding(
            REPEATED_BATCH_ID,
            f"batchId {batch.batch_id} is already recorded as signed "
            f"(BatchAlreadyProcessed / §10 replay protection); refusing to "
            f"sign it twice"))

    # ── 15/16. log integrity (§9) ───────────────────────────────────────────
    computed_log_hash = day_log.computed_log_hash
    if batch.log_hash is not None and batch.log_hash != computed_log_hash:
        findings.append(Finding(
            LOG_HASH_MISMATCH,
            f"batch.logHash {batch.log_hash[:8]}… does not match SHA-256 of "
            f"the canonical daily log (computed: {computed_log_hash}) (§9)"))
    manifest_hash = day_log.manifest_log_hash or day_log.manifest_results_sha256
    if manifest_hash and manifest_hash != computed_log_hash:
        findings.append(Finding(
            LOG_INCONSISTENT,
            f"GET /log/{day_log.day} published log_hash {manifest_hash} does "
            f"not match the canonical body (computed {computed_log_hash}) (§15)"))
    if anchored_log_hash.lower() != computed_log_hash:
        findings.append(Finding(
            ANCHOR_HASH_MISMATCH,
            f"anchored log hash {anchored_log_hash} for day {day_log.day} does "
            f"not match the canonical log (computed {computed_log_hash}) (§9)"))

    # ── entries the recomputation expects but the batch omits ───────────────
    batch_recipients = set(recipients)
    for recipient, e in expected_by_recipient.items():
        if recipient not in batch_recipients:
            findings.append(Finding(
                MISSING_PAYABLE_ENTRY,
                f"wallet {recipient[:8]}… ({e.amount_raw} raw, result_ids "
                f"{e.result_ids}) is payable per the public log but missing "
                f"from the batch", wallet=recipient))

    # ── identity: batch id and canonical batch hash (§3, §4) ────────────────
    if day != batch.day:
        findings.append(Finding(
            BATCH_ID_MISMATCH,
            f"batchId {batch.batch_id} decodes to day {day}, but the batch "
            f"declares day {batch.day} (§3)"))
    expected_id = compute_batch_id(batch.day, seq)
    if batch.batch_id != expected_id:
        findings.append(Finding(
            BATCH_ID_MISMATCH,
            f"batchId {batch.batch_id} != day*1000+seq = {expected_id} (§3)"))

    recomputed_hash: str | None = None
    try:
        recomputed_hash = batch_hash_hex(
            program_id=settings.program_id, life_mint=settings.life_mint,
            bid=batch.batch_id, day=batch.day, entries=batch.entry_pairs())
    except ValueError as exc:
        findings.append(Finding(
            BATCH_HASH_MISMATCH, f"batch hash could not be recomputed: {exc}"))
    else:
        # Only meaningful when the batch targets our program/mint at all.
        if (batch.program_id or "") == settings.program_id and \
           (batch.life_mint or "") == settings.life_mint and \
           recomputed_hash != batch.batch_hash:
            findings.append(Finding(
                BATCH_HASH_MISMATCH,
                f"batchHash mismatch: recomputed {recomputed_hash} over the "
                f"§4 preimage (tag‖program‖mint‖batchId‖day‖count‖entries), "
                f"batch claims {batch.batch_hash} (§4)"))

    # ── totals and per-wallet caps ──────────────────────────────────────────
    if batch.total_raw != entry_sum:
        findings.append(Finding(
            TOTAL_NOT_SUM_OF_ENTRIES,
            f"totalRaw {batch.total_raw} != sum of wallet entry amounts "
            f"{entry_sum}; validator fees are excluded from totalRaw (§4, §6.3)"))

    for wallet, n in per_wallet_results.items():
        if n > C.DAILY_MINER_CAP:
            findings.append(Finding(
                DAILY_MINER_CAP_EXCEEDED,
                f"wallet {wallet[:8]}… is paid {n} results in this batch, above "
                f"DAILY_MINER_CAP={C.DAILY_MINER_CAP} (§5.2)", wallet=wallet))

    if seq > 0 and expected_chunk is None:
        findings.append(Finding(
            CHUNK_SEQUENCE_INVALID,
            f"batch declares seq={seq} but no chunk plan was supplied; a "
            f"non-zero chunk can only be verified as part of a whole-day run (§5.4)"))

    # ── warnings (never sufficient to sign or refuse) ───────────────────────
    if any(isinstance(e.get("validatorFees"), list) and
           any(isinstance(f, dict) and "validatorWallet" in f
               for f in e["validatorFees"])
           for e in batch.entries):
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
    settings: Settings,
    targets: TargetsTable,
    signed_batch_ids: set[int],
    paid_result_ids: set[int] | None = None,
    now_ms: int | None = None,
) -> list[VerifyResult]:
    """Verify ALL of a day's chunks together (§5.4).

    No chunk is signable unless every chunk of the day passes: the chunks
    together must stay inside CAP_PER_DAY and no result may be paid twice
    across chunks.
    """
    results: list[VerifyResult] = []
    chunks = computation.chunks()

    cross: dict[int, list[int]] = {}
    for b in batches:
        for ent in b.entries:
            for r in (ent.get("results") or []):
                if isinstance(r, dict):
                    rid = _as_int(r.get("resultId"))
                    if rid is not None:
                        cross.setdefault(rid, []).append(b.batch_id)
    dupes = {rid: ids for rid, ids in cross.items() if len(ids) > 1}

    day_total = sum(b.total_raw for b in batches)

    for b in batches:
        _, seq = split_batch_id(b.batch_id)
        expected_chunk = chunks[seq] if seq < len(chunks) else []
        res = verify_batch(
            b, computation, day_log, anchored_log_hash, settings, targets,
            signed_batch_ids, paid_result_ids,
            day_total_other_chunks=day_total - b.total_raw,
            expected_chunk=expected_chunk, now_ms=now_ms,
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
