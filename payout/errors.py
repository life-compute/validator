"""Refusal codes for the payout signer.

Every code here means "do not sign".  There is no severity ladder that lets a
batch through: the verifier collects every finding it can and signs only when
the finding list is empty (fail closed).  WARN_* codes are informational and
recorded in the decision log, but they never permit a signature on their own
and never suppress a refusal.
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


# ── Refusal codes ────────────────────────────────────────────────────────────
NO_CONFIRMED_RESULT = "NO_CONFIRMED_RESULT"
ENTRY_AMOUNT_MISMATCH = "ENTRY_AMOUNT_MISMATCH"
ENTRY_SET_MISMATCH = "ENTRY_SET_MISMATCH"
ENTRY_ORDER_MISMATCH = "ENTRY_ORDER_MISMATCH"
ENTRY_RECIPIENT_MISMATCH = "ENTRY_RECIPIENT_MISMATCH"
ENTRY_FIELD_MISMATCH = "ENTRY_FIELD_MISMATCH"
DUPLICATE_RESULT_IN_BATCH = "DUPLICATE_RESULT_IN_BATCH"
DUPLICATE_RESULT_ACROSS_CHUNKS = "DUPLICATE_RESULT_ACROSS_CHUNKS"
RESULT_ALREADY_PAID = "RESULT_ALREADY_PAID"
ENTRY_COUNT_ABOVE_LIMIT = "ENTRY_COUNT_ABOVE_LIMIT"
BATCH_TOTAL_ABOVE_CAP = "BATCH_TOTAL_ABOVE_CAP"
DAY_TOTAL_ABOVE_CAP = "DAY_TOTAL_ABOVE_CAP"
TOTAL_NOT_SUM_OF_ENTRIES = "TOTAL_NOT_SUM_OF_ENTRIES"
DAILY_MINER_CAP_EXCEEDED = "DAILY_MINER_CAP_EXCEEDED"
BATCH_HASH_MISMATCH = "BATCH_HASH_MISMATCH"
BATCH_ID_MISMATCH = "BATCH_ID_MISMATCH"
BATCH_ID_ALREADY_SIGNED = "BATCH_ID_ALREADY_SIGNED"
LOG_HASH_MISMATCH = "LOG_HASH_MISMATCH"
ANCHOR_HASH_MISMATCH = "ANCHOR_HASH_MISMATCH"
WALLET_FLAGGED = "WALLET_FLAGGED"
WALLET_IN_HOLDBACK = "WALLET_IN_HOLDBACK"
RESULT_FORFEITED = "RESULT_FORFEITED"
UNKNOWN_TARGET_MODALITY = "UNKNOWN_TARGET_MODALITY"
VALIDATOR_FEE_MISMATCH = "VALIDATOR_FEE_MISMATCH"
COORDINATOR_UNREACHABLE = "COORDINATOR_UNREACHABLE"
LOG_INCONSISTENT = "LOG_INCONSISTENT"
CHUNK_SEQUENCE_INVALID = "CHUNK_SEQUENCE_INVALID"
SPEC_SILENT = "SPEC_SILENT"
WALLET_BIRTH_UNPROVABLE = "WALLET_BIRTH_UNPROVABLE"

# ── Warning codes (recorded, never sufficient to sign or to refuse) ──────────
WARN_FEE_RECIPIENTS_UNVERIFIABLE = "WARN_FEE_RECIPIENTS_UNVERIFIABLE"
WARN_SPEC_CONFLICT = "WARN_SPEC_CONFLICT"


class Finding:
    """One reason a batch cannot be signed."""

    __slots__ = ("code", "message", "result_id")

    def __init__(self, code: str, message: str, result_id: int | None = None):
        self.code = code
        self.message = message
        self.result_id = result_id

    def as_dict(self) -> dict:
        d: dict[str, object] = {"code": self.code, "message": self.message}
        if self.result_id is not None:
            d["result_id"] = self.result_id
        return d

    def __repr__(self) -> str:
        return f"<Finding {self.code}: {self.message}>"
