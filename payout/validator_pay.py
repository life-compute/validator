"""Validator pay (BATCH_SPEC §6.3-new), behind VALIDATOR_PAY_FROM_DAY.

Two rule-sets, selected by day number and nothing else:

  day <  VALIDATOR_PAY_FROM_DAY   §6.3-legacy.  The 7.5% fee, floored, one
                                  per confirming validator, NOT in totalRaw
                                  and NOT in the hash preimage.  Byte for
                                  byte what the checker did before C46 — this
                                  module contributes nothing at all to such a
                                  day, so the old path cannot move.
  day >= VALIDATOR_PAY_FROM_DAY   §6.3-new.  Each of the first
                                  K = VALIDATORS_PER_RESULT validators of a
                                  CONFIRMED result is paid the FULL miner
                                  amount for that result; amounts ARE part of
                                  totalRaw and therefore of the hash.

§6.3-new, point by point:

  * V(r) is the DISTINCT validator wallets whose canonical-log verdict for r
    is confirm + confirmed_hit, in canonical-log order (archived_at asc, then
    result_id asc), truncated to the first K.
  * A wallet equal to the result's miner wallet is EXCLUDED from V(r) —
    self-verification is never paid, and the exclusion happens BEFORE the
    truncation to K, so excluding one does not shrink the paid set.
  * validatorAmountRaw(r, w) = finalReward(r), the full miner amount.
  * ONE ENTRY PER WALLET: a wallet that both mines and validates gets a
    single entry whose amountRaw is the sum of both contributions, with each
    result listed in `results[]` carrying a `role` of "miner" or "validator".
  * totalRaw = Σ amountRaw over ALL entries, miner and validator alike.
  * The caps (CAP_PER_BATCH / CAP_PER_DAY) and the chunk size
    (MAX_BATCH_ENTRIES) apply to the combined set.

Refusal codes, added AFTER rule 17 in the order §6.3-new lists them:

    validator_not_verified   a paid validator has no confirmed verdict for
                             that result in the batch day's canonical log
    validator_self_pay       the paid validator is that result's miner
    validator_over_limit     more than K validators paid for one result
    validator_pay_mismatch   a validator amountRaw != the miner finalReward
                             for the same result

NO WALL CLOCK: the day is the injected batch day, as everywhere else here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import constants as C
from .batch_hash import pubkey_bytes

# §6.3-new default.
VALIDATORS_PER_RESULT = 2

# The day from which validator pay applies.  99999 = OFF (devnet).
VALIDATOR_PAY_OFF = 99_999

ROLE_MINER = "miner"
ROLE_VALIDATOR = "validator"


def validator_pay_active(day: int,
                         validator_pay_from_day: int = VALIDATOR_PAY_OFF) -> bool:
    """§6.3: the new rule applies on and after the flag day, never before."""
    return int(day) >= int(validator_pay_from_day)


@dataclass(frozen=True)
class ConfirmedVerdict:
    """One confirm + confirmed_hit verdict, as the canonical log orders it."""
    result_id: int
    validator_wallet: str
    archived_at: int

    def sort_key(self) -> tuple[int, int]:
        return (self.archived_at, self.result_id)


@dataclass
class ValidatorShare:
    """What one validator wallet is owed for one result."""
    result_id: int
    validator_wallet: str
    miner_wallet: str
    amount_raw: int


@dataclass
class ValidatorPayPlan:
    """The day's validator entries, recomputed from the log."""
    day: int
    shares: list[ValidatorShare] = field(default_factory=list)
    # result_id -> the ordered V(r) actually paid
    paid_validators: dict[int, list[str]] = field(default_factory=dict)
    # result_id -> validators excluded for being that result's miner
    self_excluded: dict[int, list[str]] = field(default_factory=dict)

    def by_wallet(self) -> dict[str, int]:
        """wallet -> summed validator amountRaw (one entry per wallet)."""
        out: dict[str, int] = {}
        for s in self.shares:
            out[s.validator_wallet] = out.get(s.validator_wallet, 0) + s.amount_raw
        return out

    def amount_for(self, result_id: int, wallet: str) -> int | None:
        for s in self.shares:
            if s.result_id == result_id and s.validator_wallet == wallet:
                return s.amount_raw
        return None

    @property
    def total_raw(self) -> int:
        return sum(s.amount_raw for s in self.shares)


def build_validator_pay_plan(
    *,
    day: int,
    confirmed: Sequence[ConfirmedVerdict],
    miner_reward_by_result: dict[int, int],
    miner_wallet_by_result: dict[int, str],
    validators_per_result: int = VALIDATORS_PER_RESULT,
) -> ValidatorPayPlan:
    """Recompute §6.3-new's validator entries for one day.

    `confirmed` is every confirm + confirmed_hit verdict of the day;
    `miner_reward_by_result` is the §6.2 finalReward the INDEPENDENT
    recomputation produced for each result (never a number from the batch).
    A result the recomputation does not pay contributes no validator pay
    either — there is no reward to mirror.
    """
    plan = ValidatorPayPlan(day=int(day))

    per_result: dict[int, list[ConfirmedVerdict]] = {}
    for v in sorted(confirmed, key=lambda c: c.sort_key()):
        per_result.setdefault(v.result_id, []).append(v)

    for result_id, verdicts in sorted(per_result.items()):
        amount = miner_reward_by_result.get(result_id)
        if amount is None:
            continue                      # not payable: nothing to mirror
        miner_wallet = miner_wallet_by_result.get(result_id, "")

        ordered: list[str] = []
        excluded: list[str] = []
        for v in verdicts:                # already in canonical-log order
            if v.validator_wallet == miner_wallet:
                # Exclusion comes BEFORE the K cut: a self-verifying wallet
                # must not consume one of the K paid slots.
                if v.validator_wallet not in excluded:
                    excluded.append(v.validator_wallet)
                continue
            if v.validator_wallet in ordered:
                continue                  # DISTINCT wallets only
            ordered.append(v.validator_wallet)

        paid = ordered[:int(validators_per_result)]
        plan.paid_validators[result_id] = paid
        if excluded:
            plan.self_excluded[result_id] = excluded
        for wallet in paid:
            plan.shares.append(ValidatorShare(
                result_id=result_id, validator_wallet=wallet,
                miner_wallet=miner_wallet, amount_raw=int(amount)))

    return plan


# ── combined entries (one wallet, one entry) ────────────────────────────────
@dataclass
class CombinedEntry:
    """One wallet's whole day: miner results + validator results, summed."""
    recipient: str
    miner_amount_raw: int = 0
    validator_amount_raw: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def amount_raw(self) -> int:
        return self.miner_amount_raw + self.validator_amount_raw


def combine_entries(miner_entries: Sequence[Any],
                    plan: ValidatorPayPlan) -> list[CombinedEntry]:
    """§6.3-new entry formation: merge miner and validator amounts per wallet.

    `miner_entries` are the ComputedEntry objects of `payout.recompute`.
    Output is sorted by recipient pubkey bytes ascending (§5.5), which is the
    order the §4 hash preimage requires.
    """
    combined: dict[str, CombinedEntry] = {}

    for e in miner_entries:
        entry = combined.setdefault(e.recipient, CombinedEntry(e.recipient))
        for r in e.results:
            entry.miner_amount_raw += r.amount_raw
            entry.results.append({
                "resultId": r.result_id, "role": ROLE_MINER,
                "amountRaw": r.amount_raw, "target": r.target,
                "archivedAt": r.archived_at})

    for s in plan.shares:
        entry = combined.setdefault(s.validator_wallet,
                                    CombinedEntry(s.validator_wallet))
        entry.validator_amount_raw += s.amount_raw
        entry.results.append({
            "resultId": s.result_id, "role": ROLE_VALIDATOR,
            "amountRaw": s.amount_raw, "minerWallet": s.miner_wallet})

    for entry in combined.values():
        # §5.5 within an entry: archived_at asc where known, then resultId.
        entry.results.sort(key=lambda r: (r.get("archivedAt", 0),
                                          int(r["resultId"]),
                                          r["role"]))
    return sorted(combined.values(), key=lambda e: pubkey_bytes(e.recipient))


def total_raw(entries: Sequence[CombinedEntry]) -> int:
    """§6.3-new: validator amounts ARE in totalRaw."""
    return sum(e.amount_raw for e in entries)


# ── legacy rule (days below the flag) ───────────────────────────────────────
def legacy_validator_fee(miner_reward: int) -> int:
    """§6.3-legacy: floor(finalReward × 750 / 10000), excluded from totalRaw.

    Delegates to the existing constant so there is ONE implementation of the
    7.5% fee and the old days cannot drift from it.
    """
    return C.validator_fee(miner_reward)
