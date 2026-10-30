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
