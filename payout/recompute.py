"""Independent recomputation of a day's payable entries from the public log.

This module never looks at the coordinator's batch.  It reads the canonical
daily log plus the feed, applies BATCH_SPEC.md §5 and §6, and returns the
entries the day SHOULD pay, in the spec's order.  The verifier then compares
that result against what the coordinator asked us to sign.

Rule map (BATCH_SPEC.md):
  §5.1  eligibility: confirmed-only (a line in the canonical log IS a
        confirmed verdict record per §9), wallet not flagged, result not
        forfeited, result not already paid, wallet-age holdback.
  §5.2  daily miner cap: 20 paid results per wallet per day.
  §5.3  CAP_PER_BATCH / CAP_PER_DAY, both 5,000 LIFE.
  §5.4  chunking at MAX_BATCH_ENTRIES=5.
  §5.5  ordering: archived_at asc, then result_id asc.
  §6    reward = taper(base(target)) ; validator fee = 7.5%, floored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import constants as C
from .errors import (
    Finding,
    SPEC_SILENT,
    UNKNOWN_TARGET_MODALITY,
    WALLET_BIRTH_UNPROVABLE,
)
from .sources import DayLog, LogRecord, OperatorInputs


@dataclass
class ComputedEntry:
    """One entry the day should pay, per spec."""
    result_id: int
    recipient: str
    amount_raw: int
    target: str
    molecule_hash: str
    archived_at: int
    validator_fee_raw: int
    hit_count_before: int
    base_reward: int
    taper_num: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "resultId": self.result_id,
            "recipient": self.recipient,
            "amountRaw": str(self.amount_raw),
            "target": self.target,
            "moleculeHash": self.molecule_hash,
        }


@dataclass
class DayComputation:
    day: int
    entries: list[ComputedEntry]
    skipped: dict[int, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    hit_counts_start: dict[str, int] = field(default_factory=dict)

    @property
    def total_raw(self) -> int:
        return sum(e.amount_raw for e in self.entries)

    def by_result_id(self) -> dict[int, ComputedEntry]:
        return {e.result_id: e for e in self.entries}

    def chunks(self) -> list[list[ComputedEntry]]:
        """§5.4: split into MAX_BATCH_ENTRIES-sized chunks, order preserved."""
        n = C.MAX_BATCH_ENTRIES
        return [self.entries[i:i + n] for i in range(0, len(self.entries), n)]


def build_submitted_at_index(feed_rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """molecule_hash -> submitted_at, from GET /feed (API_CONTRACT §2.9).

    The feed is wallet-scrubbed, so the wallet comes from the canonical log
    and the timestamp from the feed, joined on molecule_hash.
    """
    out: dict[str, int] = {}
    for row in feed_rows:
        mh = row.get("molecule_hash")
        sa = row.get("submitted_at")
        if isinstance(mh, str) and isinstance(sa, (int, float)):
            out[mh] = int(sa)
    return out


def wallet_birth_ms(wallet: str,
                    records: Sequence[LogRecord],
                    submitted_at: dict[str, int]) -> int | None:
    """§5.1.3 walletBirthMs = MIN(submitted_at) over the wallet's accepted results.

    Returns None when no submitted_at is known for ANY of the wallet's
    results — the holdback is then unprovable and the caller must refuse
    rather than assume the wallet is mature.
    """
    stamps = [submitted_at[r.molecule_hash]
              for r in records
              if r.wallet == wallet and r.molecule_hash in submitted_at]
    return min(stamps) if stamps else None


def compute_day(
    day_log: DayLog,
    submitted_at: dict[str, int],
    now_ms: int,
    operator: OperatorInputs,
    already_paid_result_ids: set[int] | None = None,
    prior_hit_counts: dict[str, int] | None = None,
    carried_in: Sequence[LogRecord] = (),
) -> DayComputation:
    """Recompute the payable entries for one calendar day.

    `carried_in` are earlier-day results that hit CAP_PER_DAY and roll forward
    (§5.3 carry-over); they keep their original queue position via archived_at.
    """
    already_paid = set(already_paid_result_ids or ())
    hit_counts = dict(prior_hit_counts or {})
    findings: list[Finding] = []
    skipped: dict[int, str] = {}

    records = list(carried_in) + list(day_log.records)

    # Duplicate result_ids inside one log day are an inconsistency, not a
    # mergeable condition.
    seen: set[int] = set()
    for r in records:
        if r.result_id in seen:
            findings.append(Finding(
                SPEC_SILENT,
                f"result_id {r.result_id} appears more than once in the day's "
                f"log; BATCH_SPEC.md defines no merge rule",
                r.result_id))
        seen.add(r.result_id)

    # §5.5 ordering: archived_at asc, then result_id asc.
    records.sort(key=lambda r: (r.archived_at, r.result_id))

    birth_cache: dict[str, int | None] = {}
    per_wallet_paid: dict[str, int] = {}
    entries: list[ComputedEntry] = []
    day_total = 0

    for rec in records:
        # §5.1.6 — already paid.
        if rec.result_id in already_paid:
            skipped[rec.result_id] = "already paid (paid_results)"
            continue
        # §5.1.5 — forfeited.
        if rec.result_id in operator.forfeited_result_ids:
            skipped[rec.result_id] = "forfeited (wallet_holdback_forfeit)"
            continue
        # §5.1.4 — flagged wallet.
        if rec.wallet in operator.flagged_wallets:
            skipped[rec.result_id] = "wallet flagged (wallet_flags.flagged=1)"
            continue

        # §5.1.3 — wallet-age holdback.
        if rec.wallet not in birth_cache:
            birth_cache[rec.wallet] = wallet_birth_ms(rec.wallet, records, submitted_at)
        birth = birth_cache[rec.wallet]
        sub = submitted_at.get(rec.molecule_hash)
        if birth is None or sub is None:
            findings.append(Finding(
                WALLET_BIRTH_UNPROVABLE,
                f"result_id {rec.result_id}: no submitted_at available for "
                f"wallet {rec.wallet[:8]}… — the §5.1.3 wallet-age holdback "
                f"cannot be evaluated from the public log",
                rec.result_id))
            skipped[rec.result_id] = "holdback unprovable"
            continue

        is_first_week = sub < birth + C.HOLDBACK_MS
        wallet_mature = (now_ms - birth) >= C.HOLDBACK_MS
        if is_first_week and not wallet_mature:
            skipped[rec.result_id] = "held: first-week result, wallet still in holdback"
            continue

        # §6.1 — base reward.  Unknown target/modality is a refusal.
        modality = operator.target_modality.get(rec.target)
        try:
            base = C.base_reward_for_target(rec.target, modality)
        except KeyError:
            findings.append(Finding(
                UNKNOWN_TARGET_MODALITY,
                f"result_id {rec.result_id}: target {rec.target!r} has no named "
                f"reward override and no modality in the operator input "
                f"({operator.source_path or 'not supplied'}); BATCH_SPEC.md §6.1 "
                f"gives no way to derive it from the public log",
                rec.result_id))
            skipped[rec.result_id] = "unknown target modality"
            continue

        # §6.2 — hit-count taper, counted per target across the stream.
        hcb = hit_counts.get(rec.target, 0)
        amount = C.final_reward(base, hcb)
        hit_counts[rec.target] = hcb + 1

        # §5.2 — daily miner cap.
        if per_wallet_paid.get(rec.wallet, 0) >= C.DAILY_MINER_CAP:
            skipped[rec.result_id] = (
                f"daily miner cap reached ({C.DAILY_MINER_CAP}/day)")
            continue

        # §5.3 — per-day token cap: stop adding, carry the rest forward.
        if day_total + amount > C.CAP_PER_DAY:
            skipped[rec.result_id] = "carried to next day (CAP_PER_DAY reached)"
            continue

        entries.append(ComputedEntry(
            result_id=rec.result_id,
            recipient=rec.wallet,
            amount_raw=amount,
            target=rec.target,
            molecule_hash=rec.molecule_hash,
            archived_at=rec.archived_at,
            validator_fee_raw=C.validator_fee(amount),
            hit_count_before=hcb,
            base_reward=base,
            taper_num=C.taper_numerator(hcb),
        ))
        day_total += amount
        per_wallet_paid[rec.wallet] = per_wallet_paid.get(rec.wallet, 0) + 1

    return DayComputation(day=day_log.day, entries=entries, skipped=skipped,
                          findings=findings,
                          hit_counts_start=dict(prior_hit_counts or {}))
