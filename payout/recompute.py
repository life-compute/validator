"""Independent recomputation of a day's payable WALLET entries from the log.

This module never looks at the coordinator's batch.  It reads the canonical
daily logs (verdict lines + §13 lifecycle events) plus the SHA-verified
targets table, applies BATCH_SPEC.md §5 and §6, and returns the entries the
day SHOULD pay.  The verifier then compares that against what the coordinator
asked us to sign.

Rule map (BATCH_SPEC.md):
  §5.1  eligibility: a line in the canonical log IS a confirmed verdict (§9);
        wallet not flagged (§13.2/§13.3), results not forfeited (§13.4),
        wallet-age holdback measured in DAY numbers from wallet_first_seen
        (§13.1, §5.1.3) — never from a wall clock.
  §5.2  daily miner cap: 20 paid results per wallet per day.
  §5.3  CAP_PER_BATCH / CAP_PER_DAY, both 5,000 LIFE.
  §5.4  each wallet appears at most ONCE per day; chunks of MAX_BATCH_ENTRIES=4.
  §5.5  wallet entries sorted by recipient pubkey bytes ascending; a wallet's
        results[] sorted by archived_at ascending.
  §6    reward = taper(base(tier from targets_table.json)); fee = 7.5% floored.
  §6a   the taper baseline is a FULL replay of the canonical logs from
        GENESIS_DAY (20000) to the day before the batch day, in order.

C5b2: wallet age, flags and forfeits come ONLY from the log's events, and
modality/tier ONLY from targets_table.json.  There are no operator-supplied
payout facts.

C5b4 (C5j): two changes with teeth.

  * NO CLOCK.  `now_ms` is gone from every signature in this module.  The
    holdback decision is `batchDay < firstSeenDay + HOLDBACK_DAYS` (§5.1.3),
    computed from the injected batch day and the log's own
    `wallet_first_seen.time_ms`.  §2 forbids `Date.now()` or any equivalent
    for holdback, forfeit and eligibility logic, so nothing here reads one.
  * REPLAY, NOT CACHE.  §6a requires the hit count to be derived by replaying
    every canonical log from GENESIS_DAY through the batch day.  The
    checker's own recorded per-day counts are a CACHE ONLY: they are compared
    against the replay and can therefore only ever cause a refusal, never
    supply a number.  A day missing from the replay range is a refusal too —
    an unverifiable baseline is never silently assumed to be zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import constants as C
from .batch_hash import pubkey_bytes
from .errors import Finding, MISSING_ARCHIVE_DAY, TAPER_CACHE_MISMATCH
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
    # Per-target confirmed-hit totals contributed by THIS day's canonical log.
    # Persisted next to the signed-batch record so the next day's taper never
    # has to ask the coordinator how many hits a target already had (§6.2).
    day_target_hits: dict[str, int] = field(default_factory=dict)
    # The baseline this day started from: target -> hits confirmed on all
    # STRICTLY EARLIER days, derived from those days' canonical logs.
    carried_in_hits: dict[str, int] = field(default_factory=dict)

    @property
    def total_raw(self) -> int:
        return sum(e.amount_raw for e in self.entries)

    def by_recipient(self) -> dict[str, ComputedEntry]:
        return {e.recipient: e for e in self.entries}

    def chunks(self) -> list[list[ComputedEntry]]:
        """§5.4: split wallet entries into MAX_BATCH_ENTRIES-sized chunks."""
        n = C.MAX_BATCH_ENTRIES
        return [self.entries[i:i + n] for i in range(0, len(self.entries), n)]


def per_day_target_hits(day_log: DayLog) -> dict[str, int]:
    """Confirmed hits per target contributed by ONE day's canonical log (§9)."""
    out: dict[str, int] = {}
    for rec in day_log.records:
        out[rec.target] = out.get(rec.target, 0) + 1
    return out


def hit_counts_before(
    history: Sequence[DayLog],
    *,
    day: int,
) -> tuple[dict[int, int], dict[str, int]]:
    """§6a/§6.2 hitCountBefore per result_id, by REPLAYING the canonical logs.

    The count is built from the canonical logs of all days STRICTLY EARLIER
    than `day`, taken in ascending day order, and then from `day`'s own log in
    confirmation order (archived_at asc, then result_id asc).  Later days are
    ignored: a hit confirmed tomorrow is not an *earlier* hit.

    There is no `carry_in`: §6a makes the replay the ONLY source of the count.
    The checker's own records are a cache, checked against this replay by
    `compute_day`, and are never added to it.

    Returns (result_id -> hitCountBefore, target -> hits carried into `day`).
    """
    seen: dict[str, int] = {}
    by_day: dict[int, DayLog] = {}
    for dl in history:
        by_day.setdefault(dl.day, dl)

    out: dict[int, int] = {}
    carried: dict[str, int] = {}
    for d in sorted(by_day):
        if d > day:
            continue                      # a later day is not an earlier hit
        if d == day:
            carried = dict(seen)          # snapshot the baseline for this day
        records: list[LogRecord] = sorted(
            by_day[d].records, key=lambda r: (r.archived_at, r.result_id))
        for rec in records:
            out[rec.result_id] = seen.get(rec.target, 0)
            seen[rec.target] = seen.get(rec.target, 0) + 1
    return out, carried


def replay_days_required(day: int, genesis_day: int = C.GENESIS_DAY) -> list[int]:
    """§6a: every day whose canonical log the replay must read for `day`.

    GENESIS_DAY through the day BEFORE the batch day, in ascending order, plus
    the batch day itself (whose own log supplies the day's results).  The start
    is the fixed GENESIS_DAY constant — never a runtime clock or query.
    """
    if int(day) < int(genesis_day):
        return [int(day)]
    return list(range(int(genesis_day), int(day) + 1))


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
    history: Sequence[DayLog] = (),
    already_paid_result_ids: set[int] | None = None,
    recorded_day_hits: dict[int, dict[str, int]] | None = None,
    genesis_day: int = C.GENESIS_DAY,
    holdback_days: int | None = None,
) -> DayComputation:
    """Recompute the payable wallet entries for one calendar day.

    `history` MUST contain the canonical log of every day from `genesis_day`
    through `day_log.day` (§6a).  A gap is a refusal, not a zero.  No clock
    parameter exists: the batch day is `day_log.day` and nothing else.

    `holdback_days` is the EFFECTIVE holdback resolved by `Settings`: the
    protocol constant 7 unless a hard-coded devnet program id activated
    HOLDBACK_DAYS_OVERRIDE.  None means the constant.
    """
    hb = C.HOLDBACK_DAYS if holdback_days is None else int(holdback_days)
    forfeiture_on = C.forfeiture_applies(hb)
    already_paid = set(already_paid_result_ids or ())
    hist = list(history) or [day_log]
    facts = fold_wallet_facts(hist)
    findings: list[Finding] = []
    skipped: dict[int, str] = {}

    # §6a — the taper count is a FULL replay from GENESIS_DAY.  Any day in
    # [genesis_day, day] whose log we were not given breaks the replay, and an
    # unverifiable baseline must never be silently treated as zero.
    recorded = {int(k): dict(v) for k, v in (recorded_day_hits or {}).items()}
    fetched_days = {dl.day for dl in hist}
    required = replay_days_required(day_log.day, genesis_day)
    missing = [d for d in required if d not in fetched_days]
    if missing:
        findings.append(Finding(
            MISSING_ARCHIVE_DAY,
            f"the §6a taper replay needs the canonical log of every day from "
            f"GENESIS_DAY={genesis_day} through {day_log.day}, but "
            f"{len(missing)} day(s) are missing from the archive: "
            f"{missing[:10]}{'…' if len(missing) > 10 else ''} — the "
            f"confirmed-hit baseline cannot be derived, so this batch cannot "
            f"be signed (§6a replay rule)"))

    hcb_map, carried_in_hits = hit_counts_before(hist, day=day_log.day)

    # §6a cache rule: anything this checker kept is ONLY a cache and must
    # equal the replay.  It is never added to the count — it can only refuse.
    for dl in sorted(hist, key=lambda d: d.day):
        if dl.day > day_log.day:
            continue
        want = recorded.get(dl.day)
        if want is None:
            continue                       # nothing cached for that day
        got = per_day_target_hits(dl)
        if got != want:
            findings.append(Finding(
                TAPER_CACHE_MISMATCH,
                f"cached confirmed-hit counts for day {dl.day} are "
                f"{sorted(want.items())}, but replaying that day's canonical "
                f"log yields {sorted(got.items())} — saved state is only a "
                f"cache and must equal the replay, so the taper baseline for "
                f"day {day_log.day} cannot be trusted (§6a cache rule)"))

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
        # §5.1.5 / §13.4 — forfeiture is permanent.  It forfeits FIRST-WEEK
        # results, so with an effective holdback of 0 there is no first week
        # and nothing to forfeit (devnet override only).
        if wf.forfeited and forfeiture_on:
            skipped[rec.result_id] = "forfeited (holdback_forfeit)"
            continue
        # §5.1.3 — wallet-age holdback, in DAY numbers: held on batch day D
        # when D < firstSeenDay + HOLDBACK_DAYS.  No wall clock takes part.
        if C.is_held_on_day(wf.first_seen_ms, day_log.day, hb):
            skipped[rec.result_id] = (
                f"held: batch day {day_log.day} < firstSeenDay "
                f"{C.day_of_ms(wf.first_seen_ms)} + "
                f"HOLDBACK_DAYS={hb}")
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
                          hit_counts_before=hcb_map,
                          day_target_hits=per_day_target_hits(day_log),
                          carried_in_hits=carried_in_hits)
