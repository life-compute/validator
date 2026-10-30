"""Payout constants — taken ONLY from /root/api/c5b-bundle/BATCH_SPEC.md.

Nothing here is copied from the old validator (no tier tables, no commission
function, no required-validators constant).  Every value below cites the
section of BATCH_SPEC.md (or its README constant table) that defines it.

C5b2: the spec no longer contradicts itself on MAX_BATCH_ENTRIES or the token
caps, so there is no SPEC_CONFLICTS list and no WARN_SPEC_CONFLICT warning.
"""
from __future__ import annotations

# ── Token units (§5.3) ───────────────────────────────────────────────────────
RAW_PER_LIFE = 1_000_000          # 1 LIFE = 1e6 raw units, 6 decimals

# ── Caps (§5.3; README constant table) ───────────────────────────────────────
CAP_PER_BATCH = 5_000_000_000     # 5,000 LIFE, per single payout_batch call
CAP_PER_DAY = 5_000_000_000       # 5,000 LIFE, cumulative over ALL chunks/day

# ── Entry limits (§5.4, §5.6, §14, README) ───────────────────────────────────
# §14: N=5 serialises to 1,243B > 1,232B MTU; N=4 fits with 62B spare.
MAX_BATCH_ENTRIES = 4             # distinct WALLET entries per on-chain batch
DAILY_MINER_CAP = 20              # paid results per miner wallet per day (§5.2)

# ── Holdback (§5.1.3; README) ────────────────────────────────────────────────
HOLDBACK_MS = 604_800_000         # 7 days

# ── Reward tiers (§12 tier -> base reward; mirrors §6.1) ─────────────────────
REWARD_HARD = 900_000             # tier "hard"   (protein)
REWARD_MRNA = 900_000             # tier "mrna"
REWARD_CRISPR = 252_000           # tier "crispr"
REWARD_EASY = 300_000             # tier "easy"   (small_molecule)

# §12: "The builder reads the modality and tier only from this table."
# Tier is the authoritative key; modality is carried for reporting only.
BASE_REWARD_BY_TIER = {
    "hard": REWARD_HARD,
    "mrna": REWARD_MRNA,
    "crispr": REWARD_CRISPR,
    "easy": REWARD_EASY,
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
DOMAIN_TAG = b"LIFE-PAYOUT-V1"    # §4, 14 bytes UTF-8, no NUL

# ── targets_table.json integrity (§12) ───────────────────────────────────────
TARGETS_TABLE_SHA256 = (
    "07c97ecd63db314cabd4883371eec89837f678cd3dc91fe7151ebcc19c3e7e8f"
)


def base_reward_for_tier(tier: str) -> int:
    """§12: tier -> base reward.  An unknown tier is a refusal, never a guess."""
    try:
        return BASE_REWARD_BY_TIER[tier]
    except KeyError:
        raise KeyError(f"unknown tier {tier!r}") from None


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
