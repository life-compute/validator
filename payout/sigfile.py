"""Signature file (§C5r.1) — build, serialise, verify.

Schema accepted by `POST /payout/{day}/signature`:

    {
      "signerPubkeyHex": "<hex Ed25519 public key>",
      "chunks": [ { "batchId": "<decimal u64 string>",
                    "signatureHex": "<128-char hex Ed25519 signature>" } ]
    }

Byte format: `json.dumps(obj, indent=2)` with NO trailing newline and with the
key order above.  That is the exact encoding of the bundle's sample signature
files (verified byte-for-byte), so the file this module writes is the file the
route accepts.

The signature is over `hex_decode(batchHash)` — the RAW 32 bytes of the
SHA-256 batch hash, never the 64-char hex string (§C5r.1, §8).

BUNDLE DEFECT (do not "fix" by matching it): the bundle's sample online
signature files were produced with a REUSED Ed25519 nonce — all three days
carry the identical R value over three different batch hashes, which leaks the
signing key (the scalar is recoverable from any two of them, and day 20002's S
is exactly predictable from days 20000+20001).  This module signs with
standard RFC 8032 deterministic Ed25519, so its output verifies under the same
key but is NOT byte-identical to those samples.  Reproducing them would mean
shipping a signer that discloses its own private key.  See
tests/test_payout_cli.py::test_bundle_sample_is_byte_identical (strict xfail).
"""
from __future__ import annotations

import json
from typing import Any

from .errors import PayoutError

SIG_HEX_LEN = 128  # 64 raw bytes


def build_signature_file(signer_pubkey_hex: str,
                         chunks: list[tuple[int, str]]) -> dict[str, Any]:
    """Assemble the §C5r.1 object.  `chunks` is [(batch_id, signature_hex)]."""
    if not chunks:
        raise PayoutError("refusing to build an empty signature file")
    out_chunks = []
    for bid, sig_hex in chunks:
        sig_hex = str(sig_hex).lower()
        if len(sig_hex) != SIG_HEX_LEN:
            raise PayoutError(
                f"signatureHex must be {SIG_HEX_LEN} hex chars (64 bytes), "
                f"got {len(sig_hex)} for batchId {bid}")
        try:
            bytes.fromhex(sig_hex)
        except ValueError:
            raise PayoutError(
                f"signatureHex for batchId {bid} is not valid hex") from None
        out_chunks.append({"batchId": str(int(bid)), "signatureHex": sig_hex})
    return {"signerPubkeyHex": str(signer_pubkey_hex).lower(),
            "chunks": out_chunks}


def serialize_signature_file(obj: dict[str, Any]) -> bytes:
    """The exact on-disk / on-wire bytes: indent=2, no trailing newline."""
    return json.dumps(obj, indent=2).encode("utf-8")
