"""Payout signing key — separate from the validator's verdict-signing key.

Threat model: a compromised verdict key must NOT be able to authorise payouts,
and a compromised payout key must not be able to issue verdicts.  Two
independent guards enforce that:

  1. PAYOUT_KEYPAIR is REQUIRED and has NO default.  Unset -> refuse to start.
  2. At startup the payout public key is compared to the validator's
     verdict-signing public key (VALIDATOR_KEYPAIR).  Equal -> refuse to start.

No secret material is ever returned, logged, stringified or placed in argv;
only public keys leave this module.  Key files are read exactly once.

REAL KEY FILE LOCATION: production reads the path in the PAYOUT_KEYPAIR
environment variable.  There is no built-in default and this service never
creates, rotates or writes a key file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from solders.keypair import Keypair

from .errors import PayoutKeyError

PAYOUT_KEYPAIR_ENV = "PAYOUT_KEYPAIR"
VALIDATOR_KEYPAIR_ENV = "VALIDATOR_KEYPAIR"


def _load_keypair(path: str | Path, label: str) -> Keypair:
    p = Path(path)
    if not p.is_file():
        raise PayoutKeyError(f"{label} file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError:
        raise PayoutKeyError(f"{label} file is not valid JSON: {p}") from None
    if not isinstance(raw, list):
        raise PayoutKeyError(f"{label} file must be a JSON array of bytes: {p}")
    kp_bytes = bytes(raw)
    if len(kp_bytes) != 64:
        raise PayoutKeyError(
            f"{label} file must contain 64 bytes (32 seed || 32 pubkey), "
            f"got {len(kp_bytes)}: {p}")
    try:
        return Keypair.from_bytes(kp_bytes)
    except Exception:
        raise PayoutKeyError(f"{label} file does not decode to an Ed25519 keypair: {p}") from None


def validator_pubkey(path: str | Path) -> str:
    """Base58 public key of the validator's verdict-signing key.  Safe to log."""
    return str(_load_keypair(path, "VALIDATOR_KEYPAIR").pubkey())


class PayoutSigner:
    """Signs the 32 raw bytes of a batch hash (BATCH_SPEC §8) — nothing else."""

    def __init__(self, keypair: Keypair, key_path: str | None = None):
        self._kp = keypair
        self.key_path = key_path

    # ── construction with the start-up guards ───────────────────────────────
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "PayoutSigner":
        env = dict(os.environ if env is None else env)

        payout_path = (env.get(PAYOUT_KEYPAIR_ENV) or "").strip()
        if not payout_path:
            raise PayoutKeyError(
                f"{PAYOUT_KEYPAIR_ENV} is required and has no default; "
                f"the payout signer refuses to start without it")

        payout_kp = _load_keypair(payout_path, PAYOUT_KEYPAIR_ENV)
        payout_pub = str(payout_kp.pubkey())

        validator_path = (env.get(VALIDATOR_KEYPAIR_ENV) or "").strip()
        if not validator_path:
            raise PayoutKeyError(
                f"{VALIDATOR_KEYPAIR_ENV} is required so the payout key can be "
                f"proven DIFFERENT from the verdict-signing key; refusing to start")

        if str(Path(payout_path).resolve()) == str(Path(validator_path).resolve()):
            raise PayoutKeyError(
                "PAYOUT_KEYPAIR and VALIDATOR_KEYPAIR point at the same file; "
                "the payout key must be a separate key so one compromised key "
                "cannot both issue verdicts and authorise payouts")

        validator_pub = validator_pubkey(validator_path)
        if payout_pub == validator_pub:
            raise PayoutKeyError(
                f"PAYOUT_KEYPAIR public key {payout_pub} is identical to the "
                f"validator's verdict-signing key; the payout key must be a "
                f"separate key so one compromised key cannot do both")

        return cls(payout_kp, key_path=payout_path)

    @classmethod
    def from_keypair_file(cls, path: str | Path,
                          validator_keypair_path: str | Path) -> "PayoutSigner":
        return cls.from_env({
            PAYOUT_KEYPAIR_ENV: str(path),
            VALIDATOR_KEYPAIR_ENV: str(validator_keypair_path),
        })

    # ── public identity ─────────────────────────────────────────────────────
    @property
    def pubkey(self) -> str:
        """Base58 payout public key.  Safe to log."""
        return str(self._kp.pubkey())

    # ── signing ─────────────────────────────────────────────────────────────
    def sign_batch_hash(self, batch_hash: bytes) -> str:
        """§8: sign the 32 RAW bytes of SHA-256(preimage). Returns hex (128 chars)."""
        if not isinstance(batch_hash, (bytes, bytearray)) or len(batch_hash) != 32:
            raise PayoutKeyError(
                "batch hash must be the 32 raw bytes of SHA-256(preimage), "
                "not the hex string (BATCH_SPEC §8)")
        return bytes(self._kp.sign_message(bytes(batch_hash))).hex()

    # ── leak guards ─────────────────────────────────────────────────────────
    def __repr__(self) -> str:
        return f"<PayoutSigner pubkey={self.pubkey[:8]}… (secret withheld)>"

    __str__ = __repr__
