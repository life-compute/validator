"""Independent recomputation of a day's payable WALLET entries from the log.

This module never looks at the coordinator's batch.  It reads the canonical
daily logs (verdict lines + §13 lifecycle events) plus the SHA-verified
targets table, applies BATCH_SPEC.md §5 and §6, and returns the entries the
day SHOULD pay.  The verifier then compares that against what the coordinator
asked us to sign.

Rule map (BATCH_SPEC.md):
  §5.1  eligibility: a line in the canonical log IS a confirmed verdict (§9);
        wallet not flagged (§13.2/§13.3), results not forfeited (§13.4),
        wallet-age holdback measured from wallet_first_seen (§13.1).
  §5.2  daily miner cap: 20 paid results per wallet per day.
  §5.3  CAP_PER_BATCH / CAP_PER_DAY, both 5,000 LIFE.
  §5.4  each wallet appears at most ONCE per day; chunks of MAX_BATCH_ENTRIES=4.
  §5.5  wallet entries sorted by recipient pubkey bytes ascending; a wallet's
        results[] sorted by archived_at ascending.
  §6    reward = taper(base(tier from targets_table.json)); fee = 7.5% floored.

C5b2: wallet age, flags and forfeits come ONLY from the log's events, and
modality/tier ONLY from targets_table.json.  There are no operator-supplied
payout facts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import constants as C
from .batch_hash import pubkey_bytes
from .errors import Finding
from .sources import DayLog, LogRecord, TargetsTable, WalletFacts


@dataclass
class ComputedResult:
    """One result inside a wallet's entry (the per-result breakdown)."""
    result_id: int
    target: str
    tier: str
    molecule_hash: str
    archived_at: int
    amount_raw: int
    validator_fee_raw: int
    hit_count_before: int
    base_reward: int
    taper_num: int


@dataclass
class ComputedEntry:
    """One WALLET's total for the day (§5.4 aggregation)."""
    recipient: str
    results: list[ComputedResult] = field(default_factory=list)

    @property
    def amount_raw(self) -> int:
        return sum(r.amount_raw for r in self.results)

    @property
    def validator_fee_raw(self) -> int:
        return sum(r.validator_fee_raw for r in self.results)

    @property
    def result_ids(self) -> list[int]:
        return [r.result_id for r in self.results]

    def by_result_id(self) -> dict[int, ComputedResult]:
        return {r.result_id: r for r in self.results}


@dataclass
class DayComputation:
    day: int
    entries: list[ComputedEntry]
    skipped: dict[int, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    wallet_facts: dict[str, WalletFacts] = field(default_factory=dict)
    hit_counts_before: dict[int, int] = field(default_factory=dict)

    @property
    def total_raw(self) -> int:
        return sum(e.amount_raw for e in self.entries)

    def by_recipient(self) -> dict[str, ComputedEntry]:
        return {e.recipient: e for e in self.entries}

    def chunks(self) -> list[list[ComputedEntry]]:
        """§5.4: split wallet entries into MAX_BATCH_ENTRIES-sized chunks."""
        n = C.MAX_BATCH_ENTRIES
        return [self.entries[i:i + n] for i in range(0, len(self.entries), n)]


def hit_counts_before(history: Sequence[DayLog]) -> dict[int, int]:
    """§6.2 hitCountBefore per result_id, counted across the whole history.

    A "confirmed hit" is a verdict line in a canonical log (§9).  Results are
    walked in confirmation order (archived_at asc, then result_id asc) and
    each one records how many confirmed hits that target already had.
    """
    records: list[LogRecord] = [r for dl in history for r in dl.records]
    records.sort(key=lambda r: (r.archived_at, r.result_id))
    seen: dict[str, int] = {}
    out: dict[int, int] = {}
    for rec in records:
        out[rec.result_id] = seen.get(rec.target, 0)
        seen[rec.target] = seen.get(rec.target, 0) + 1
    return out


def fold_wallet_facts(history: Sequence[DayLog]) -> dict[str, WalletFacts]:
    """Fold §13 lifecycle events across every day up to the target day.

    wallet_first_seen is emitted once, on the wallet's first day, so a later
    day's batch can only be checked against the whole history.
    """
    facts: dict[str, WalletFacts] = {}
    for dl in sorted(history, key=lambda d: d.day):
        facts = dl.wallet_facts(carry_in=facts)
    return facts


def compute_day(
    day_log: DayLog,
    *,
    targets: TargetsTable,
    now_ms: int,
    history: Sequence[DayLog] = (),
    already_paid_result_ids: set[int] | None = None,
) -> DayComputation:
    """Recompute the payable wallet entries for one calendar day."""
    already_paid = set(already_paid_result_ids or ())
    hist = list(history) or [day_log]
    facts = fold_wallet_facts(hist)
    hcb_map = hit_counts_before(hist)
    findings: list[Finding] = []
    skipped: dict[int, str] = {}

    records = sorted(day_log.records, key=lambda r: (r.archived_at, r.result_id))

    per_wallet: dict[str, ComputedEntry] = {}
    per_wallet_count: dict[str, int] = {}
    day_total = 0

    for rec in records:
        wf = facts.get(rec.wallet, WalletFacts(wallet=rec.wallet))

        # §5.1.6 — already paid.
        if rec.result_id in already_paid:
            skipped[rec.result_id] = "already paid (paid_results)"
            continue
        # §13.1 — wallet age must be provable from the log.
        if wf.first_seen_ms is None:
            skipped[rec.result_id] = "no wallet_first_seen event in the log"
            continue
        # §5.1.4 / §13.2 — flagged wallet.
        if wf.flagged:
            skipped[rec.result_id] = "wallet flagged (wallet_flagged, no wallet_unflagged)"
            continue
        # §5.1.5 / §13.4 — forfeiture is permanent.
        if wf.forfeited:
            skipped[rec.result_id] = "forfeited (holdback_forfeit)"
            continue
        # §5.1.3 — wallet-age holdback, measured from wallet_first_seen.
        if (now_ms - wf.first_seen_ms) < C.HOLDBACK_MS:
            skipped[rec.result_id] = (
                f"held: wallet age {now_ms - wf.first_seen_ms}ms < "
                f"HOLDBACK_MS={C.HOLDBACK_MS}ms")
            continue

        # §12 / §6.1 — tier comes only from targets_table.json.
        info = targets.get(rec.target)
        if info is None:
            skipped[rec.result_id] = f"target {rec.target!r} not in targets_table.json"
            continue
        base = C.base_reward_for_tier(info.tier)

        # §6.2 — hit-count taper.
        hcb = hcb_map.get(rec.result_id, 0)
        amount = C.final_reward(base, hcb)

        # §5.2 — daily miner cap.
        if per_wallet_count.get(rec.wallet, 0) >= C.DAILY_MINER_CAP:
            skipped[rec.result_id] = f"daily miner cap reached ({C.DAILY_MINER_CAP}/day)"
            continue

        # §5.3 — per-day token cap: stop adding, carry the rest forward.
        if day_total + amount > C.CAP_PER_DAY:
            skipped[rec.result_id] = "carried to next day (CAP_PER_DAY reached)"
            continue

        entry = per_wallet.setdefault(rec.wallet, ComputedEntry(recipient=rec.wallet))
        entry.results.append(ComputedResult(
            result_id=rec.result_id,
            target=rec.target,
            tier=info.tier,
            molecule_hash=rec.molecule_hash,
            archived_at=rec.archived_at,
            amount_raw=amount,
            validator_fee_raw=C.validator_fee(amount),
            hit_count_before=hcb,
            base_reward=base,
            taper_num=C.taper_numerator(hcb),
        ))
        day_total += amount
        per_wallet_count[rec.wallet] = per_wallet_count.get(rec.wallet, 0) + 1

    # §5.5 — results within a wallet: archived_at asc; wallets: pubkey bytes asc.
    for entry in per_wallet.values():
        entry.results.sort(key=lambda r: (r.archived_at, r.result_id))
    entries = sorted(per_wallet.values(), key=lambda e: pubkey_bytes(e.recipient))

    return DayComputation(day=day_log.day, entries=entries, skipped=skipped,
                          findings=findings, wallet_facts=facts,
                          hit_counts_before=hcb_map)
