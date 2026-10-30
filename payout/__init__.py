"""Independent payout-batch verifier and online signer (C5b / C5b2).

Recomputes every payout batch from the PUBLIC log before signing, using only
the rules in /root/api/c5b-bundle/BATCH_SPEC.md.  Nothing in this package
imports the old validator's tier tables, commission function or
required-validators constant.

Fail closed: no recomputation, no match, no source, no key -> no signature.
"""
from .batch_hash import (
    batch_hash_bytes,
    batch_hash_hex,
    batch_id,
    batch_preimage,
    log_hash_hex,
    pubkey_bytes,
)
from .errors import (
    CoordinatorUnreachable,
    Finding,
    PayoutConfigError,
    PayoutError,
    PayoutKeyError,
)
from .ledger import DecisionLedger
from .recompute import compute_day, ComputedEntry, DayComputation
from .service import DayDecision, PayoutSignerService
from .signer_key import PayoutSigner
from .sources import (
    AnchorSource,
    DayLog,
    LogSource,
    Settings,
    TargetsTable,
    WalletFacts,
)
from .summary import summarize_day
from .verifier import Batch, RULE_PRECEDENCE, verify_batch, verify_day, VerifyResult

__all__ = [
    "AnchorSource", "Batch", "ComputedEntry", "CoordinatorUnreachable",
    "DayComputation", "DayDecision", "DayLog", "DecisionLedger", "Finding",
    "LogSource", "PayoutConfigError", "PayoutError", "PayoutKeyError",
    "PayoutSigner", "PayoutSignerService", "RULE_PRECEDENCE", "Settings",
    "TargetsTable", "VerifyResult", "WalletFacts", "batch_hash_bytes",
    "batch_hash_hex", "batch_id", "batch_preimage", "compute_day",
    "log_hash_hex", "pubkey_bytes", "summarize_day", "verify_batch",
    "verify_day",
]
