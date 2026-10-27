"""LIFE Compute — coordinator validator client (C2).

Replaces the on-chain WebSocket/catch-up validator loop with a pull-based
coordinator client:  GET /results/pending  →  re-score  →  POST /verdicts.

Signing is pure Python (Ed25519 via solders); the Node hop is gone.
"""

__all__ = ["signing", "config", "contract", "progress", "hashpin", "rescore", "client"]
