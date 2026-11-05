"""Refusal codes for the payout signer.

Every code here means "do not sign".  There is no severity ladder that lets a
batch through: the verifier evaluates rules in a fixed precedence and signs
only when no rule fired (fail closed).  WARN_* codes are informational and
recorded in the decision log, but they never permit a signature on their own
and never suppress a refusal.

C5b2: WARN_SPEC_CONFLICT is gone — BATCH_SPEC.md no longer contradicts itself
on MAX_BATCH_ENTRIES or on the 5,000 LIFE caps.  WARN_FEE_RECIPIENTS_
UNVERIFIABLE remains, because the canonical log still carries no
validator_wallet field.

Rule names match the `rule` field of the bundle's .verdict.json fixtures so a
verdict can be compared to the published expectation by name.
"""
from __future__ import annotations


class PayoutError(RuntimeError):
    """Base class for payout-signer failures."""


class CoordinatorUnreachable(PayoutError):
    """The coordinator / archive / anchor source could not be read.

    Raised, never swallowed: an unreachable source produces NO signature.
    """


class DayNotClosed(CoordinatorUnreachable):
    """The day is still OPEN, so its log is not final and must not be used.

    BATCH_SPEC §13.6 / API_CONTRACT §2.16: an open day answers
    `GET /log/{day}/canonical` with `409 Conflict` and `GET /log/{day}` with
    `closed: false`.  An open day's lines can still change, so no payout may
    ever be derived from one.  This is a subclass of CoordinatorUnreachable
    so that every existing fail-closed path still catches it, but it carries
    its own refusal code so the reason is explicit rather than a bare
    "HTTP 409".
    """


class AnchoredHashMismatch(CoordinatorUnreachable):
    """The canonical log does not hash to the ANCHORED value (§9).

    Split out from the generic "source could not be read" case so the refusal
    is named `anchor_hash_mismatch` rather than the vaguer `log_inconsistent`:
    with on-chain anchors (C44b) this is the one failure that means the chain
    and the coordinator genuinely disagree, and it must be distinguishable
    from a published-hash inconsistency inside the coordinator's own answer.
    It stays a CoordinatorUnreachable subclass so every existing fail-closed
    path still catches it.
    """


class PayoutKeyError(PayoutError):
    """Refuse-to-start condition for the payout signing key."""


class PayoutConfigError(PayoutError):
    """Missing or invalid required configuration (PROGRAM_ID, LIFE_MINT, table)."""


# ── Refusal codes (names match the bundle's verdict fixtures) ────────────────
WRONG_PROGRAM_ID_OR_MINT = "wrong_program_id_or_mint"
BATCH_TOTAL_ABOVE_CAP = "batch_total_above_cap"
TOTAL_ABOVE_DAY_CAP = "total_above_day_cap"
DUPLICATE_RECIPIENT = "duplicate_recipient"
SORT_CONTRACT_VIOLATED = "sort_contract_violated"
INFLATED_AMOUNT = "inflated_amount"
TARGET_NOT_IN_TABLE = "target_not_in_table"
NO_WALLET_FIRST_SEEN = "no_wallet_first_seen"
FLAGGED_WALLET_PAID = "flagged_wallet_paid"
FORFEITED_WALLET_PAID = "forfeited_wallet_paid"
HELD_WALLET_PAID = "held_wallet_paid"
NO_CONFIRMED_RESULT = "no_confirmed_result"
REPEATED_BATCH_ID = "repeated_batch_id"
LOG_HASH_MISMATCH = "log_hash_mismatch"
CHUNK_ABOVE_ENTRY_LIMIT = "chunk_above_entry_limit"
REWARD_MISMATCH = "reward_mismatch"
VALIDATOR_FEE_MISMATCH = "validator_fee_mismatch"
MISSING_PAYABLE_ENTRY = "missing_payable_entry"
TOTAL_NOT_SUM_OF_ENTRIES = "total_not_sum_of_entries"
BATCH_HASH_MISMATCH = "batch_hash_mismatch"
ANCHOR_HASH_MISMATCH = "anchor_hash_mismatch"
BATCH_ID_MISMATCH = "batch_id_mismatch"
DUPLICATE_RESULT_IN_BATCH = "duplicate_result_in_batch"
DUPLICATE_RESULT_ACROSS_CHUNKS = "duplicate_result_across_chunks"
RESULT_ALREADY_PAID = "result_already_paid"
DAILY_MINER_CAP_EXCEEDED = "daily_miner_cap_exceeded"
RECIPIENT_NOT_IN_LOG = "recipient_not_in_log"
CHUNK_SEQUENCE_INVALID = "chunk_sequence_invalid"
LOG_INCONSISTENT = "log_inconsistent"
MALFORMED_BATCH = "malformed_batch"
COORDINATOR_UNREACHABLE = "coordinator_unreachable"
NO_PAYOUT_KEY = "no_payout_key"
# §6a — the taper baseline must be a full replay from GENESIS_DAY.  A day that
# the archive cannot serve breaks the replay, and any state the checker kept is
# only a cache that must equal the replay.  Both are refusals.
MISSING_ARCHIVE_DAY = "missing_archive_day"
TAPER_CACHE_MISMATCH = "taper_cache_mismatch"
# §13.6 — an OPEN day is not final: GET /log/{day}/canonical answers 409 and
# GET /log/{day} reports closed:false.  Its log may still change, so it is
# never read, never cached and never signed against.
DAY_NOT_CLOSED = "day_not_closed"
# §6.1 / §10a — the reference reward and the weekly discovery bonus are not
# payable through payout_batch at all, so a batch carrying either is refused.
OUT_OF_SCOPE_REWARD_PAID = "out_of_scope_reward_paid"

# ── §6.3-new (C46 validator pay, behind VALIDATOR_PAY_FROM_DAY) ────────────
# Added to SPEC_RULE_ORDER *after* rule 17 (total_mismatch), in the order
# §6.3-new lists them.  They can never fire for a day below the flag day,
# where the legacy 7.5% fee rule applies unchanged.
VALIDATOR_NOT_VERIFIED = "validator_not_verified"
VALIDATOR_SELF_PAY = "validator_self_pay"
VALIDATOR_OVER_LIMIT = "validator_over_limit"
VALIDATOR_PAY_MISMATCH = "validator_pay_mismatch"

# ── §17 (C47 log format v2, behind LOG_V2_FROM_DAY) ────────────────────────
# The day's signed request log did not satisfy a v2 rule.  The specific
# lower_snake_case code lives in payout.log_v2 and is carried in the
# finding's message; this is the entry in the batch-level rule order.
LOG_V2_REFUSED = "log_v2_refused"

# ── Warning codes (recorded, never sufficient to sign or to refuse) ─────────
WARN_FEE_RECIPIENTS_UNVERIFIABLE = "warn_fee_recipients_unverifiable"


class Finding:
    """One reason a batch cannot be signed."""

    __slots__ = ("code", "message", "result_id", "wallet")

    def __init__(self, code: str, message: str,
                 result_id: int | None = None, wallet: str | None = None):
        self.code = code
        self.message = message
        self.result_id = result_id
        self.wallet = wallet

    def as_dict(self) -> dict:
        d: dict[str, object] = {"code": self.code, "message": self.message}
        if self.result_id is not None:
            d["result_id"] = self.result_id
        if self.wallet is not None:
            d["wallet"] = self.wallet
        return d

    def __repr__(self) -> str:
        return f"<Finding {self.code}: {self.message}>"
