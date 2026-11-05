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

import re

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

# ── Devnet-only holdback override (NOT part of BATCH_SPEC) ──────────────────
# The devnet coordinator is deployed with a holdback of 0 so a test day can be
# paid the moment it closes.  HOLDBACK_DAYS_OVERRIDE lets the checker follow
# that deployment, but ONLY for a program id that is hard-coded here: the
# tuple below is the complete allow-list and is deliberately NOT configurable,
# so no environment variable, file or flag can make a real cluster accept a
# holdback other than the protocol constant 7.
#
# HOLDBACK_DAYS_OVERRIDE is read instead of HOLDBACK_DAYS: the plain
# HOLDBACK_DAYS environment variable stays ignored on every network.
DEVNET_PROGRAM_IDS = (
    "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv",
    "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf",
)

HOLDBACK_OVERRIDE_ENV = "HOLDBACK_DAYS_OVERRIDE"

_INT_RE = re.compile(r"^[+-]?[0-9]+$")


def is_devnet_program_id(program_id: str) -> bool:
    """True only for an EXACT match against the hard-coded devnet allow-list.

    Exact string equality: a program id that differs by a single character is
    a different program and gets the protocol constant, not the override.
    """
    return isinstance(program_id, str) and program_id in DEVNET_PROGRAM_IDS


def parse_holdback_override(raw: object) -> int:
    """Parse HOLDBACK_DAYS_OVERRIDE.  Raises ValueError on anything invalid.

    Accepts a non-negative decimal integer only — no floats ("0.0"), no
    whitespace-only, no "true", no negative value.  Validation happens at
    start-up and BEFORE the program id is consulted, so a malformed setting is
    refused on devnet and on a real cluster alike.
    """
    if isinstance(raw, bool):
        raise ValueError(f"{raw!r} is not an integer")
    if isinstance(raw, int):
        value = int(raw)
    else:
        text = str(raw).strip()
        if not text or not _INT_RE.match(text):
            raise ValueError(f"{str(raw)!r} is not an integer")
        value = int(text)
    if value < 0:
        raise ValueError(f"{value} is below 0")
    return value

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

# ── C47 / C46 activation flags (BATCH_SPEC §17, §6.3) ───────────────────────
# Both are DAY NUMBERS and both default to 99999 = OFF, which is the devnet
# deployment's setting.  They are never derived from a clock: a day number is
# configuration, exactly like GENESIS_DAY.
#
#   LOG_V2_FROM_DAY        first day that uses log format v2 (signed.ndjson,
#                          anchor /2, every §17 rule).
#   VALIDATOR_PAY_FROM_DAY first day that pays validators the full miner
#                          amount (§6.3-new).  Below it the legacy 7.5% fee
#                          rule applies, byte for byte as before.
FLAG_OFF_DAY = 99_999
LOG_V2_FROM_DAY_DEFAULT = FLAG_OFF_DAY
VALIDATOR_PAY_FROM_DAY_DEFAULT = FLAG_OFF_DAY

LOG_V2_FROM_DAY_ENV = "LOG_V2_FROM_DAY"
VALIDATOR_PAY_FROM_DAY_ENV = "VALIDATOR_PAY_FROM_DAY"


def parse_flag_day(raw: object, name: str) -> int:
    """Parse an activation flag day.  Raises ValueError on anything invalid.

    A non-negative decimal integer only — no floats, no booleans, no empty
    string.  A malformed value refuses start-up on every network rather than
    silently falling back to OFF, because "OFF" and "misconfigured" must not
    look the same.
    """
    if isinstance(raw, bool):
        raise ValueError(f"{name}={raw!r} is not an integer")
    if isinstance(raw, int):
        value = int(raw)
    else:
        text = str(raw).strip()
        if not text or not _INT_RE.match(text):
            raise ValueError(f"{name}={str(raw)!r} is not an integer")
        value = int(text)
    if value < 0:
        raise ValueError(f"{name}={value} is below 0")
    return value


def validate_flag_days(log_v2_from_day: int, validator_pay_from_day: int,
                       genesis_day: int = GENESIS_DAY) -> None:
    """§17/§6.3 ordering rules.  Raises ValueError with the exact reason.

    Two invariants, both refusals at start-up:

      1. Neither flag may name a day BELOW the genesis day.  A day before the
         network existed has no canonical log at all, so a flag pointing
         there could never be satisfied and would silently refuse every day.
      2. VALIDATOR_PAY_FROM_DAY must not be below LOG_V2_FROM_DAY.  Validator
         pay is verified from the signed verdict log (§6.3-new reads V(r) out
         of the canonical log and the §17 signed lines prove who verified);
         paying validators on a day whose log is still v1 would pay against
         evidence the checker cannot verify.

    The OFF value (99999) is above every real day and above GENESIS_DAY, so
    the default configuration satisfies both automatically.
    """
    for name, day in ((LOG_V2_FROM_DAY_ENV, int(log_v2_from_day)),
                      (VALIDATOR_PAY_FROM_DAY_ENV, int(validator_pay_from_day))):
        if day < int(genesis_day):
            raise ValueError(
                f"{name}={day} is below GENESIS_DAY={genesis_day}: the "
                f"network has no canonical log before its first day, so this "
                f"flag could never be satisfied. Refusing to start.")
    if int(validator_pay_from_day) < int(log_v2_from_day):
        raise ValueError(
            f"{VALIDATOR_PAY_FROM_DAY_ENV}={int(validator_pay_from_day)} is "
            f"below {LOG_V2_FROM_DAY_ENV}={int(log_v2_from_day)}: validator "
            f"pay (§6.3-new) is verified from the v2 signed verdict log, so "
            f"it can never start before log format v2 does. Refusing to start.")


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


def holdback_release_day(first_seen_ms: int,
                         holdback_days: int | None = None) -> int:
    """First batch day on which a wallet's holdback has expired (§5.1.3).

    `holdback_days` defaults to the protocol constant 7.  It is only ever
    something else when `Settings` resolved a devnet override (see
    DEVNET_PROGRAM_IDS); callers on a real cluster always get 7.
    """
    hb = HOLDBACK_DAYS if holdback_days is None else int(holdback_days)
    return day_of_ms(first_seen_ms) + hb


def is_held_on_day(first_seen_ms: int, batch_day: int,
                   holdback_days: int | None = None) -> bool:
    """§5.0 rule 12 / §5.1.3: held on batch day D when D < firstSeenDay + 7.

    No wall-clock value takes part: `first_seen_ms` is the log's own
    wallet_first_seen timestamp and `batch_day` is the injected batch day.
    With an effective holdback of 0 the condition D < firstSeenDay + 0 is
    false for every wallet, i.e. there is no first week at all.
    """
    return int(batch_day) < holdback_release_day(first_seen_ms, holdback_days)


def forfeiture_applies(holdback_days: int | None = None) -> bool:
    """§5.1.5 / §13.4: is there a first week whose results can be forfeited?

    `holdback_forfeit` forfeits a wallet's FIRST-WEEK results.  When the
    effective holdback is 0 there is no first week, so nothing can be
    forfeited and the rule is consistently inert — otherwise a devnet batch
    could be refused for forfeiting a window the same config says is empty.
    """
    hb = HOLDBACK_DAYS if holdback_days is None else int(holdback_days)
    return hb > 0

