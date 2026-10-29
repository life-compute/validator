"""Payout constants — taken ONLY from /root/api/c5b-bundle/BATCH_SPEC.md.

Nothing here is copied from the old validator (no tier tables, no commission
function, no required-validators constant).  Every value below cites the
section of BATCH_SPEC.md (or its README constant table) that defines it.

Where the spec contradicts itself the stricter reading is used and the
contradiction is recorded in SPEC_CONFLICTS so the operator can see it.
"""
from __future__ import annotations

# ── Token units (§5.3) ───────────────────────────────────────────────────────
RAW_PER_LIFE = 1_000_000          # 1 LIFE = 1e6 raw units, 6 decimals

# ── Caps (§5.3; README constant table) ───────────────────────────────────────
CAP_PER_BATCH = 5_000_000_000     # 5,000 LIFE, per single payout_batch call
CAP_PER_DAY = 5_000_000_000       # 5,000 LIFE, cumulative over ALL chunks/day

# ── Entry limits (§5.4, §5.6, README) ────────────────────────────────────────
MAX_BATCH_ENTRIES = 5             # per on-chain batch call
DAILY_MINER_CAP = 20              # paid results per miner wallet per day (§5.2)

# ── Holdback (§5.1.3; README) ────────────────────────────────────────────────
HOLDBACK_MS = 604_800_000         # 7 days

# ── Reward table (§6.1; README) ──────────────────────────────────────────────
REWARD_HARD = 900_000             # protein
REWARD_MRNA = 900_000
REWARD_CRISPR = 252_000
REWARD_EASY = 300_000             # small_molecule

BASE_REWARD_BY_MODALITY = {
    "protein": REWARD_HARD,
    "mrna": REWARD_MRNA,
    "crispr": REWARD_CRISPR,
    "small_molecule": REWARD_EASY,
}

# §6.1: "Named targets override modality lookup (currently: CDK4=Hard, MYC=Hard)."
NAMED_TARGET_BASE_REWARD = {
    "CDK4": REWARD_HARD,
    "MYC": REWARD_HARD,
}

# ── Hit-count taper (§6.2) ───────────────────────────────────────────────────
# finalReward = max(1, floor(baseReward * l2Num / 4))
TAPER_TIERS = (
    (0, 99, 4),
    (100, 999, 3),
    (1000, None, 2),
)

# ── Validator fee (§6.3) ─────────────────────────────────────────────────────
VALIDATOR_FEE_BPS = 750           # 7.5% of the miner reward, floor()
BPS_DENOM = 10_000

# ── Batch id / hash (§3, §4) ─────────────────────────────────────────────────
BATCH_ID_DAY_MULTIPLIER = 1_000   # batchId = day * 1000 + seq

# ── Spec contradictions the operator must be aware of ────────────────────────
SPEC_CONFLICTS = (
    "BATCH_SPEC.md §12 states MAX_BATCH_ENTRIES=20, contradicting §5.4, §5.6 "
    "and the bundle README (both say 5). The stricter value 5 is enforced.",
    "BATCH_SPEC.md §12 states 'There is no global 5,000-result per-batch or "
    "per-day cap', contradicting §5.3 which defines CAP_PER_BATCH and "
    "CAP_PER_DAY as 5,000 LIFE. The §5.3 caps are enforced.",
)


def base_reward_for_target(target: str, modality: str | None) -> int:
    """Resolve the base reward for a target (§6.1).

    Named overrides win.  Otherwise the modality must be supplied by the
    operator's target->modality map; the public log carries no modality field
    and BATCH_SPEC.md defines no way to derive one, so an unknown target is a
    refusal (never a guess).
    """
    if target in NAMED_TARGET_BASE_REWARD:
        return NAMED_TARGET_BASE_REWARD[target]
    if modality is None:
        raise KeyError(target)
    try:
        return BASE_REWARD_BY_MODALITY[modality]
    except KeyError:
        raise KeyError(f"{target}:{modality}") from None


def taper_numerator(hit_count_before: int) -> int:
    """l2Num for a given prior-confirmed-hit count (§6.2)."""
    for lo, hi, num in TAPER_TIERS:
        if hit_count_before >= lo and (hi is None or hit_count_before <= hi):
            return num
    raise ValueError(f"no taper tier for hit_count_before={hit_count_before}")


def final_reward(base: int, hit_count_before: int) -> int:
    """§6.2: finalReward = max(1, floor(baseReward * l2Num / 4)) — integer math."""
    return max(1, (base * taper_numerator(hit_count_before)) // 4)


def validator_fee(miner_reward: int) -> int:
    """§6.3: floor(finalReward * 750 / 10000). Paid per confirming validator."""
    return (miner_reward * VALIDATOR_FEE_BPS) // BPS_DENOM


def raw_to_life(raw: int) -> str:
    """Human-readable LIFE amount, exact (no float)."""
    whole, frac = divmod(int(raw), RAW_PER_LIFE)
    return f"{whole:,}.{frac:06d}".rstrip("0").rstrip(".") if frac else f"{whole:,}"
