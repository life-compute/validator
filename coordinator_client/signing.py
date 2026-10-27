"""Ed25519 request signing for the coordinator API (API_CONTRACT.md §1, §7.2).

Canonical message (UTF-8, newline-separated, no trailing newline):

    <METHOD>\n<PATH>\n<body_sha256_hex>\n<nonce>\n<timestamp>

- METHOD    uppercase HTTP verb
- PATH      pathname only; no query string, no trailing slash
- body hash lowercase hex SHA-256 of the RAW request body bytes
            (GET -> zero bytes -> e3b0c442...b855)
- nonce     32 lowercase hex chars (16 random bytes)
- timestamp Unix epoch SECONDS (integer)

Signature: 64 raw Ed25519 bytes, lowercase hex (128 chars).
Wallet:    32-byte Ed25519 public key, Solana base58.

SECURITY INVARIANTS enforced here and asserted by tests:
  * No secret key material is ever returned, logged, stringified or placed in
    argv.  Only the public key leaves this module.
  * The keypair file is read exactly once, at construction.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

import base58
from solders.keypair import Keypair

# SHA-256 of zero bytes — the body hash every GET must sign over.
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_body(body_obj: Any | None) -> bytes:
    """Serialise a request body to the exact bytes that get hashed and sent.

    None -> zero bytes (GET).  Anything else -> compact JSON, matching the
    contract's `JSON.stringify` / `separators=(",", ":")` reference.
    """
    if body_obj is None:
        return b""
    return json.dumps(body_obj, separators=(",", ":")).encode()


def build_message(method: str, path: str, body_bytes: bytes,
                  nonce: str, timestamp: int) -> bytes:
    return f"{method}\n{path}\n{sha256_hex(body_bytes)}\n{nonce}\n{timestamp}".encode()


class Signer:
    """Holds one Ed25519 keypair and produces X-Auth headers.

    The private key stays inside the solders Keypair object.  `__repr__` is
    overridden so an accidental log/format of this object cannot leak it.
    """

    def __init__(self, keypair: Keypair, worker_name: str | None = None):
        self._kp = keypair
        self.worker_name = worker_name

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_keypair_file(cls, path: str | Path, worker_name: str | None = None) -> "Signer":
        """Load a Solana CLI keypair JSON (64-byte array: 32 seed || 32 pubkey)."""
        raw = json.loads(Path(path).read_text())
        kp_bytes = bytes(raw)
        if len(kp_bytes) != 64:
            raise ValueError(
                f"keypair file must contain 64 bytes, got {len(kp_bytes)}"
            )
        return cls(Keypair.from_bytes(kp_bytes), worker_name)

    @classmethod
    def generate(cls, worker_name: str | None = None) -> "Signer":
        """Throwaway in-memory key — tests only, never written to disk."""
        return cls(Keypair(), worker_name)

    # ── public identity ─────────────────────────────────────────────────────
    @property
    def wallet(self) -> str:
        """Base58 Solana public key (43-44 chars).  Safe to log."""
        return str(self._kp.pubkey())

    @property
    def pubkey_bytes(self) -> bytes:
        return bytes(self._kp.pubkey())

    # ── signing ─────────────────────────────────────────────────────────────
    def sign_message(self, message: bytes) -> str:
        """Return the lowercase-hex 64-byte Ed25519 signature over *message*."""
        return bytes(self._kp.sign_message(message)).hex()

    def auth_header(self, method: str, path: str, body_bytes: bytes,
                    nonce: str | None = None,
                    timestamp: int | None = None) -> str:
        """Build the JSON-encoded X-Auth header value for one request."""
        nonce = nonce or secrets.token_hex(16)
        timestamp = int(time.time()) if timestamp is None else int(timestamp)
        message = build_message(method.upper(), path, body_bytes, nonce, timestamp)
        payload: dict[str, Any] = {
            "wallet": self.wallet,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": self.sign_message(message),
        }
        if self.worker_name:
            payload["worker_name"] = self.worker_name
        return json.dumps(payload, separators=(",", ":"))

    # ── leak guards ─────────────────────────────────────────────────────────
    def __repr__(self) -> str:
        return f"<Signer wallet={self.wallet[:8]}… (secret withheld)>"

    __str__ = __repr__


def verify(wallet_b58: str, message: bytes, signature_hex: str) -> bool:
    """Verify a signature the way the coordinator does (`src/auth.ts`)."""
    from solders.pubkey import Pubkey
    from solders.signature import Signature

    try:
        pub = Pubkey.from_bytes(base58.b58decode(wallet_b58))
        sig = Signature.from_bytes(bytes.fromhex(signature_hex))
        return sig.verify(pub, message)
    except Exception:
        return False
