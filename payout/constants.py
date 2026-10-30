"""Payout constants — taken ONLY from /root/api/c5b-bundle/BATCH_SPEC.md.

Nothing here is copied from the old validator (no tier tables, no commission
function, no required-validators constant).  Every value below cites the
section of BATCH_SPEC.md (or its README constant table) that defines it.

C5b2: the spec no longer contradicts itself on MAX_BATCH_ENTRIES or the token
caps, so there is no SPEC_CONFLICTS list and no WARN_SPEC_CONFLICT warning.

C5b4 (C5j): holdback is DAY-based.  §2 forbids any implementation from calling
`Date.now()` (or any equivalent wall-clock API) for holdback, forfeit or
eligibility logic, and §5.1.3 restates the rule purely in day numbers.  This
module therefore exposes HOLDBACK_DAYS and GENESIS_DAY, and the whole `payout`
package imports no clock at all.
"""
from __future__ import annotations

# ── Token units (§5.3) ───────────────────────────────────────────────────────
RAW_PER_LIFE = 1_000_000          # 1 LIFE = 1e6 raw units, 6 decimals

# ── Calendar (§2) ────────────────────────────────────────────────────────────
# day = floor(unix_time_ms / 86_400_000).  The batch's day is ALWAYS injected
# as an explicit parameter; it is never derived from a runtime clock (§2 C5j).
MS_PER_DAY = 86_400_000

# §6a: the network's first day.  Fixed in the spec and in every fixture
# generator; an implementation MUST NOT derive it from a clock or a query.
GENESIS_DAY = 20_000

# ── Caps (§5.3; README constant table) ───────────────────────────────────────
CAP_PER_BATCH = 5_000_000_000     # 5,000 LIFE, per single payout_batch call
CAP_PER_DAY = 5_000_000_000       # 5,000 LIFE, cumulative over ALL chunks/day

# ── Entry limits (§5.4, §5.6, §14, README) ───────────────────────────────────
# §14: N=5 serialises to 1,243B > 1,232B MTU; N=4 fits with 62B spare.
MAX_BATCH_ENTRIES = 4             # distinct WALLET entries per on-chain batch
DAILY_MINER_CAP = 20              # paid results per miner wallet per day (§5.2)

# ── Holdback (§5.1.3, §5.0 rule 12; README) ─────────────────────────────────
# The README publishes the window as HOLDBACK_MS = 604,800,000 ms ("7 days").
# §5.1.3 (C5j) expresses the rule itself in DAY numbers only:
#     held on batch day D  <=>  D < firstSeenDay + HOLDBACK_DAYS
# so HOLDBACK_DAYS is the decision-bearing constant and HOLDBACK_MS is kept
# only as the published value it is derived from.
HOLDBACK_MS = 604_800_000         # 7 days, as published in the README table
HOLDBACK_DAYS = HOLDBACK_MS // MS_PER_DAY        # == 7
assert HOLDBACK_DAYS * MS_PER_DAY == HOLDBACK_MS, \
    "HOLDBACK_MS must be a whole number of days for the day-based rule"

# ── Reward tiers (§12 tier -> base reward; mirrors §6.1) ─────────────────────
# FIVE valid tiers.  "medium" is new in this spec revision and the table now
# prices protein targets by scientific difficulty (4 easy / 15 medium / 11
# hard) instead of a blanket protein->hard default (§6.1).
REWARD_EASY = 300_000             # tier "easy"    0.3   LIFE
REWARD_MEDIUM = 700_000           # tier "medium"  0.7   LIFE
REWARD_HARD = 900_000             # tier "hard"    0.9   LIFE
REWARD_MRNA = 900_000             # tier "mrna"    0.9   LIFE
REWARD_CRISPR = 252_000           # tier "crispr"  0.252 LIFE

# §12: "The builder reads the modality and tier only from this table."
# Tier is the authoritative key; modality is carried for reporting only.
BASE_REWARD_BY_TIER = {
    "easy": REWARD_EASY,
    "medium": REWARD_MEDIUM,
    "hard": REWARD_HARD,
    "mrna": REWARD_MRNA,
    "crispr": REWARD_CRISPR,
}

# ── Out of scope: never payable via payout_batch (§6.1, §10a) ───────────────
# Neither value appears in REWARD_BY_TIER.  A batch that pays either of them
# is refused outright, whatever target it names.
REWARD_REFERENCE = 108_000        # 0.108 LIFE per hit  — claimed on-chain
REWARD_DISCOVERY = 100_000_000    # 100 LIFE weekly     — claim_discovery_bonus
OUT_OF_SCOPE_REWARDS = {
    REWARD_REFERENCE: "reference compound reward (0.108 LIFE)",
    REWARD_DISCOVERY: "weekly discovery bonus (100 LIFE)",
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
# The value published in BATCH_SPEC.md §12 (file bytes, trailing newline
# included).  Verified before any target is priced.
TARGETS_TABLE_SHA256 = (
    "1c538a21eb276bfa4d1cce697d248231d9011aa4886464b7226885a0cd09128c"
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


def out_of_scope_reward(amount_raw: int) -> str | None:
    """§10a: name the out-of-scope reward an amount equals, else None."""
    return OUT_OF_SCOPE_REWARDS.get(int(amount_raw))


def raw_to_life(raw: int) -> str:
    """Human-readable LIFE amount, exact (no float)."""
    whole, frac = divmod(int(raw), RAW_PER_LIFE)
    return f"{whole:,}.{frac:06d}".rstrip("0").rstrip(".") if frac else f"{whole:,}"


# ── Day-based holdback helpers (§2, §5.1.3) ─────────────────────────────────
def day_of_ms(time_ms: int) -> int:
    """§2: day = floor(unix_time_ms / 86_400_000).

    Used ONLY to turn a timestamp that the canonical log itself carries
    (`wallet_first_seen.time_ms`, `submitted_at`) into its UTC day number.
    It never reads a clock; the value comes from the log.
    """
    return int(time_ms) // MS_PER_DAY


def holdback_release_day(first_seen_ms: int) -> int:
    """First batch day on which a wallet's holdback has expired (§5.1.3)."""
    return day_of_ms(first_seen_ms) + HOLDBACK_DAYS


def is_held_on_day(first_seen_ms: int, batch_day: int) -> bool:
    """§5.0 rule 12 / §5.1.3: held on batch day D when D < firstSeenDay + 7.

    No wall-clock value takes part: `first_seen_ms` is the log's own
    wallet_first_seen timestamp and `batch_day` is the injected batch day.
    """
    return int(batch_day) < holdback_release_day(first_seen_ms)
