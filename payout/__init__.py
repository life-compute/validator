"""Independent payout-batch verifier and online signer (C5b).

Recomputes every payout batch from the PUBLIC log before signing, using only
the rules in /root/api/c5b-bundle/BATCH_SPEC.md.  Nothing in this package
imports the old validator's tier tables, commission function or
required-validators constant.

Fail closed: no recomputation, no match, no source, no key -> no signature.
"""
from .batch_hash import batch_hash_bytes, batch_hash_hex, batch_id, log_hash_hex
from .errors import CoordinatorUnreachable, Finding, PayoutError, PayoutKeyError
from .ledger import DecisionLedger
from .recompute import compute_day, DayComputation
from .service import DayDecision, PayoutSignerService
from .signer_key import PayoutSigner
from .sources import AnchorSource, LogSource, OperatorInputs
from .summary import summarize_day
from .verifier import Batch, verify_batch, verify_day, VerifyResult

__all__ = [
    "AnchorSource", "Batch", "CoordinatorUnreachable", "DayComputation",
    "DayDecision", "DecisionLedger", "Finding", "LogSource", "OperatorInputs",
    "PayoutError", "PayoutKeyError", "PayoutSigner", "PayoutSignerService",
    "VerifyResult", "batch_hash_bytes", "batch_hash_hex", "batch_id",
    "compute_day", "log_hash_hex", "summarize_day", "verify_batch", "verify_day",
]
