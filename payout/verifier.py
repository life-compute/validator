"""Verify a coordinator-supplied batch against an independent recomputation.

Rule order is NOT ours to choose.  BATCH_SPEC §5.0 ("Checker Rule Order")
numbers the rules 1..17 and requires that a batch violating several of them is
refused for the FIRST one, so that every independent implementation returns
the same reason for the same bad batch.  RULE_PRECEDENCE below is that
numbered list, transcribed in order; nothing is reordered on judgement.

BATCH_SPEC §5.0, verbatim order:
   1 program_id_or_mint       programId/lifeMint != configured values
   2 batch_total_above_cap    totalRaw over CAP_PER_BATCH
   3 chunk_above_entry_limit  entry count over MAX_BATCH_ENTRIES (4)
   4 total_above_day_cap      day's accumulated total over CAP_PER_DAY
   5 duplicate_recipient      two entries share a recipient
   6 sort_contract_violated   entries not strictly ascending by pubkey bytes
   7 inflated_amount          amountRaw != sum(results[*].amountRaw)
   8 target_not_in_table      a result's target absent from targets_table.json
   9 no_wallet_first_seen     recipient has no wallet_first_seen event
  10 flagged_wallet_paid      wallet_flagged with no later wallet_unflagged
  11 forfeited_wallet_paid    holdback_forfeit event present
  12 held_wallet_paid         batchDay < firstSeenDay + HOLDBACK_DAYS
  13 no_confirmed_result      resultId has no confirmed verdict in the log
  14 repeated_batch_id        batchId already recorded (BatchReplay / ledger)
  15 log_hash_mismatch        batch.logHash != SHA-256(canonical log)
  16 reward_mismatch          per-result amount != calcReward(base, hitsBefore)
  17 total_mismatch           totalRaw != sum of entry amountRaw values

§6.3-new adds four more codes AFTER rule 17, in the order that section lists
them (validator_not_verified, validator_self_pay, validator_over_limit,
validator_pay_mismatch).  They are reachable ONLY for a day at or above
VALIDATOR_PAY_FROM_DAY; below it the legacy 7.5% fee rule applies and this
block is inert, so no existing day can change verdict.

NOTE on 11/12 (changed again this revision — C5b4): §5.0 rule 11 is now
`forfeited_wallet_paid` and rule 12 is `held_wallet_paid`, with the spec
stating the reason outright: "A forfeited wallet is also necessarily in its
first week, but the more specific reason is forfeiture, so this rule fires
before `held_wallet_paid`."  The previous revision had these the other way
round and needed a hand-picked checker clock to make fixture 07 reachable at
all.  With forfeiture first AND the holdback rule expressed in day numbers,
both fixtures reach their published rule with no clock anywhere: 07's wallet
(first seen day 20001, forfeited) reports forfeiture on batch day 20001, and
06's wallet (first seen day 20000) is held on batch day 20002 because
20002 < 20000 + 7.

NO CLOCK (C5j, §2): `now_ms` is gone from every signature in this module.  The
holdback decision uses the injected batch day and the log's own
`wallet_first_seen.time_ms`, so the same fixtures decide identically under any
system clock.

Codes with no §5.0 number (anchor_hash_mismatch, batch_hash_mismatch, the
cross-chunk duplicate guards, …) are checks this checker makes beyond the
spec's list.  They are ranked AFTER all 17 numbered rules so they can never
pre-empt a spec-numbered reason, while still refusing the batch.

A batch is signable only when NO rule fired.  Every finding is collected for
the decision log; `primary_rule` is the first one in this order.
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
    LOG_V2_REFUSED,
    MISSING_PAYABLE_ENTRY,
    MISSING_ARCHIVE_DAY,
    NO_CONFIRMED_RESULT,
    NO_WALLET_FIRST_SEEN,
    OUT_OF_SCOPE_REWARD_PAID,
    RECIPIENT_NOT_IN_LOG,
    REPEATED_BATCH_ID,
    RESULT_ALREADY_PAID,
    REWARD_MISMATCH,
    SORT_CONTRACT_VIOLATED,
    TARGET_NOT_IN_TABLE,
    TAPER_CACHE_MISMATCH,
    TOTAL_ABOVE_DAY_CAP,
    TOTAL_NOT_SUM_OF_ENTRIES,
    VALIDATOR_NOT_VERIFIED,
    VALIDATOR_OVER_LIMIT,
    VALIDATOR_PAY_MISMATCH,
    VALIDATOR_SELF_PAY,
    VALIDATOR_FEE_MISMATCH,
    WARN_FEE_RECIPIENTS_UNVERIFIABLE,
    WRONG_PROGRAM_ID_OR_MINT,
)
from .recompute import ComputedEntry, DayComputation
from .sources import DayLog, Settings, TargetsTable, WalletFacts
from .validator_pay import (
    ROLE_MINER,
    ROLE_VALIDATOR,
    VALIDATORS_PER_RESULT,
    ValidatorPayPlan,
)

# ── BATCH_SPEC §5.0, rules 1..17, transcribed in the spec's own order ───────
SPEC_RULE_ORDER = (
    WRONG_PROGRAM_ID_OR_MINT,    # 1  program_id_or_mint
    BATCH_TOTAL_ABOVE_CAP,       # 2
    CHUNK_ABOVE_ENTRY_LIMIT,     # 3
    TOTAL_ABOVE_DAY_CAP,         # 4
    DUPLICATE_RECIPIENT,         # 5
    SORT_CONTRACT_VIOLATED,      # 6
    INFLATED_AMOUNT,             # 7
    TARGET_NOT_IN_TABLE,         # 8
    NO_WALLET_FIRST_SEEN,        # 9
    FLAGGED_WALLET_PAID,         # 10
    FORFEITED_WALLET_PAID,       # 11  (before 12 — the spec's order, not ours)
    HELD_WALLET_PAID,            # 12
    NO_CONFIRMED_RESULT,         # 13
    REPEATED_BATCH_ID,           # 14
    LOG_HASH_MISMATCH,           # 15
    REWARD_MISMATCH,             # 16
    TOTAL_NOT_SUM_OF_ENTRIES,    # 17 total_mismatch
)

# §6.3-new — appended AFTER rule 17, in the spec's own order.  Inert for any
# day below VALIDATOR_PAY_FROM_DAY.
VALIDATOR_PAY_RULE_ORDER = (
    VALIDATOR_NOT_VERIFIED,
    VALIDATOR_SELF_PAY,
    VALIDATOR_OVER_LIMIT,
    VALIDATOR_PAY_MISMATCH,
)

# Checks this checker makes beyond §5.0's list.  Ranked strictly after every
# numbered rule so they refuse a batch without ever pre-empting a spec reason.
EXTRA_RULE_ORDER = (
    # §6a: without a complete replay from GENESIS_DAY there is no trustworthy
    # baseline at all, so these come first among the non-numbered checks.
    MISSING_ARCHIVE_DAY,
    TAPER_CACHE_MISMATCH,
    OUT_OF_SCOPE_REWARD_PAID,
    VALIDATOR_FEE_MISMATCH,
    ANCHOR_HASH_MISMATCH,
    RECIPIENT_NOT_IN_LOG,
    DUPLICATE_RESULT_IN_BATCH,
    DUPLICATE_RESULT_ACROSS_CHUNKS,
    RESULT_ALREADY_PAID,
    MISSING_PAYABLE_ENTRY,
    DAILY_MINER_CAP_EXCEEDED,
    BATCH_HASH_MISMATCH,
    BATCH_ID_MISMATCH,
    CHUNK_SEQUENCE_INVALID,
    LOG_INCONSISTENT,
    # §17 — the v2 signed-log verdict for the day, behind LOG_V2_FROM_DAY.
    LOG_V2_REFUSED,
)

RULE_PRECEDENCE = SPEC_RULE_ORDER + VALIDATOR_PAY_RULE_ORDER + EXTRA_RULE_ORDER
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
) -> VerifyResult:
    """Full per-batch verification.  Returns a VerifyResult; never raises.

    There is no clock parameter: the holdback rule (§5.0 rule 12, §5.1.3) is
    decided from the batch's own `day` and the log's wallet_first_seen event.
    """
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

        # §6.1 / §10a — an ENTRY that pays an out-of-scope reward outright.
        entry_oos = C.out_of_scope_reward(amount)
        if entry_oos is not None:
            findings.append(Finding(
                OUT_OF_SCOPE_REWARD_PAID,
                f"entry {recipient[:8]}…: amountRaw {amount} is the "
                f"{entry_oos}, which is not payable via payout_batch "
                f"(§6.1, §10a)", wallet=recipient))

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

            # §6.1 / §10a — the reference reward and the weekly discovery
            # bonus are NOT payable through payout_batch at all.  Neither
            # appears in REWARD_BY_TIER, so a result paying either is refused
            # on sight, whatever target it names.
            r_amount_raw = _as_int(r.get("amountRaw"))
            if r_amount_raw is not None:
                oos = C.out_of_scope_reward(r_amount_raw)
                if oos is not None:
                    findings.append(Finding(
                        OUT_OF_SCOPE_REWARD_PAID,
                        f"result_id {rid}: amountRaw {r_amount_raw} is the "
                        f"{oos}, which is out of scope for payout_batch — it "
                        f"appears in no tier of REWARD_BY_TIER and is claimed "
                        f"on-chain, never through this pipeline (§6.1, §10a)",
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
            if wf.forfeited and C.forfeiture_applies(settings.holdback_days):
                findings.append(Finding(
                    FORFEITED_WALLET_PAID,
                    f"recipient {recipient[:8]}… has a holdback_forfeit event "
                    f"in the canonical log — first-week results are permanently "
                    f"forfeited (§13.4, §5.1.5)", wallet=recipient))
            # §5.0 rule 12 / §5.1.3 — DAY-based holdback, evaluated against the
            # batch's own day.  No wall-clock value is read anywhere.  The
            # effective holdback comes from `settings` (7 unless a hard-coded
            # devnet program id activated HOLDBACK_DAYS_OVERRIDE).
            if C.is_held_on_day(wf.first_seen_ms, batch.day,
                                settings.holdback_days):
                findings.append(Finding(
                    HELD_WALLET_PAID,
                    f"recipient {recipient[:8]}… has wallet_first_seen "
                    f"time_ms={wf.first_seen_ms} in the canonical log "
                    f"(firstSeenDay={C.day_of_ms(wf.first_seen_ms)}); batch day "
                    f"{batch.day} < {C.day_of_ms(wf.first_seen_ms)} + "
                    f"HOLDBACK_DAYS={settings.holdback_days} = "
                    f"{C.holdback_release_day(wf.first_seen_ms, settings.holdback_days)}, so this "
                    f"first-week result must be held (§5.1.3, §5.0 rule 12)",
                    wallet=recipient))

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


# ── §6.3-new validator pay (behind VALIDATOR_PAY_FROM_DAY) ──────────────────
def verify_validator_pay(
    batch: Batch,
    plan: ValidatorPayPlan,
    *,
    validators_per_result: int = VALIDATORS_PER_RESULT,
) -> list[Finding]:
    """Check a batch's validator entries against the recomputed §6.3-new plan.

    Returns findings only; the caller merges them into the batch's own list
    so the §5.0 precedence decides the primary rule.  Called ONLY for a day
    at or above VALIDATOR_PAY_FROM_DAY: for an earlier day the legacy 7.5%
    fee rule applies and this function is never invoked, so the old days
    rebuild byte for byte.

    The four codes are evaluated in the order §6.3-new lists them.
    """
    findings: list[Finding] = []

    # Every `role: "validator"` breakdown row the batch claims.
    claimed: list[tuple[str, int, int]] = []       # (wallet, result_id, amount)
    for ent in batch.entries:
        recipient = ent.get("recipient")
        if not isinstance(recipient, str):
            continue
        for r in (ent.get("results") or []):
            if not isinstance(r, dict) or r.get("role") != ROLE_VALIDATOR:
                continue
            rid = _as_int(r.get("resultId"))
            amount = _as_int(r.get("amountRaw"))
            if rid is None or amount is None:
                findings.append(Finding(
                    VALIDATOR_NOT_VERIFIED,
                    f"entry {recipient[:8]}…: a validator result row carries "
                    f"no usable resultId/amountRaw (§6.3-new)",
                    wallet=recipient))
                continue
            claimed.append((recipient, rid, amount))

    # ── validator_not_verified ─────────────────────────────────────────────
    for wallet, rid, _amount in claimed:
        if wallet not in plan.paid_validators.get(rid, []):
            findings.append(Finding(
                VALIDATOR_NOT_VERIFIED,
                f"wallet {wallet[:8]}… is paid as a validator of result_id "
                f"{rid}, but the canonical log of day {plan.day} records no "
                f"confirm + confirmed_hit verdict from it for that result "
                f"within the first K={validators_per_result} (§6.3-new)",
                rid, wallet))

    # ── validator_self_pay ─────────────────────────────────────────────────
    # `plan.self_excluded[rid]` is exactly the set of wallets the log shows
    # self-verifying result rid, which the plan refused to pay.  A batch that
    # pays one of them is the self-pay case.
    for wallet, rid, _amount in claimed:
        if wallet in plan.self_excluded.get(rid, []):
            findings.append(Finding(
                VALIDATOR_SELF_PAY,
                f"wallet {wallet[:8]}… is paid as a validator of result_id "
                f"{rid} but is that result's miner; self-verification is "
                f"never paid (§6.3-new)", rid, wallet))

    # ── validator_over_limit ───────────────────────────────────────────────
    per_result: dict[int, set[str]] = {}
    for wallet, rid, _amount in claimed:
        per_result.setdefault(rid, set()).add(wallet)
    for rid, wallets in sorted(per_result.items()):
        if len(wallets) > validators_per_result:
            findings.append(Finding(
                VALIDATOR_OVER_LIMIT,
                f"result_id {rid} pays {len(wallets)} validators, above "
                f"K=VALIDATORS_PER_RESULT={validators_per_result} (§6.3-new)",
                rid))

    # ── validator_pay_mismatch ─────────────────────────────────────────────
    for wallet, rid, amount in claimed:
        expected = plan.amount_for(rid, wallet)
        if expected is not None and amount != expected:
            findings.append(Finding(
                VALIDATOR_PAY_MISMATCH,
                f"wallet {wallet[:8]}… is paid {amount} raw as a validator of "
                f"result_id {rid}, but §6.3-new pays the FULL miner amount "
                f"{expected} raw for that result", rid, wallet))

    # A validator the log says must be paid but the batch omits.
    for rid, wallets in sorted(plan.paid_validators.items()):
        for wallet in wallets:
            if not any(w == wallet and r == rid for w, r, _ in claimed):
                findings.append(Finding(
                    MISSING_PAYABLE_ENTRY,
                    f"wallet {wallet[:8]}… is owed the validator amount for "
                    f"result_id {rid} per §6.3-new but the batch pays it "
                    f"nothing for that result", rid, wallet))

    return findings


def verify_day(
    batches: Sequence[Batch],
    computation: DayComputation,
    day_log: DayLog,
    anchored_log_hash: str,
    settings: Settings,
    targets: TargetsTable,
    signed_batch_ids: set[int],
    paid_result_ids: set[int] | None = None,
) -> list[VerifyResult]:
    """Verify ALL of a day's chunks together (§5.4).

    No chunk is signable unless every chunk of the day passes: the chunks
    together must stay inside CAP_PER_DAY and no result may be paid twice
    across chunks.  No clock parameter: see `verify_batch`.
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
