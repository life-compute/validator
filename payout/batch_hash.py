"""Canonical batch id / batch hash (BATCH_SPEC.md §3, §4, §8, §9).

    batchId   = day * 1000 + seq                                      (§3)
    preimage  = "LIFE-PAYOUT-V1"      (14 bytes, UTF-8 domain tag)
              || program_id           (32 bytes, base58-decoded)
              || life_mint            (32 bytes, base58-decoded)
              || batchId_le8          (u64 LE)
              || day_le4              (u32 LE)
              || entry_count_u8       (u8)
              || [per entry, SORTED by recipient pubkey bytes ascending]
                   recipient_pubkey   (32 bytes raw)
                || amount_le8         (u64 LE)
    batchHash = SHA-256(preimage)                                     (§4)
    logHash   = SHA-256(canonical_daily_log_ndjson_utf8)              (§9)

Total preimage length = 91 + entry_count * 40 bytes.

Each entry is ONE WALLET's total for the day (§5.4 aggregation), not one
result.  Validator fees are excluded from the hashed amounts (§6.3).

Signers sign the 32 RAW bytes of batchHash, not the hex string (§8).

PROGRAM_ID and LIFE_MINT are REQUIRED configuration (§16) — this module takes
them as explicit arguments and has no defaults, so a checker configured with
the wrong chain constants cannot silently verify a batch.
"""
from __future__ import annotations

import hashlib
import struct
from typing import Iterable, Sequence

from .constants import BATCH_ID_DAY_MULTIPLIER, DOMAIN_TAG

U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(s: str) -> bytes:
    """Base58 (Bitcoin alphabet) decode, preserving leading-zero bytes."""
    if not isinstance(s, str) or not s:
        raise ValueError("base58 input must be a non-empty string")
    n = 0
    for c in s:
        try:
            n = n * 58 + _B58_INDEX[c]
        except KeyError:
            raise ValueError(f"invalid base58 character {c!r}") from None
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def pubkey_bytes(b58: str) -> bytes:
    """Decode a base58 Ed25519 public key to exactly 32 raw bytes (§4)."""
    raw = b58decode(b58)
    if len(raw) != 32:
        raise ValueError(
            f"public key must decode to 32 bytes, got {len(raw)}: {b58[:12]}…")
    return raw


def batch_id(day: int, seq: int) -> int:
    """§3: batchId = BigInt(day) * 1000n + BigInt(seq)."""
    if day < 0 or day > U32_MAX:
        raise ValueError(f"day out of u32 range: {day}")
    if seq < 0:
        raise ValueError(f"seq must be non-negative: {seq}")
    return day * BATCH_ID_DAY_MULTIPLIER + seq


def split_batch_id(bid: int) -> tuple[int, int]:
    """Inverse of batch_id(): -> (day, seq)."""
    return divmod(int(bid), BATCH_ID_DAY_MULTIPLIER)


def is_sorted_ascending(recipients: Sequence[str]) -> bool:
    """§4 sort contract: STRICTLY ascending by raw pubkey bytes.

    Strict (`<`, not `<=`) so a repeated recipient is also a violation.
    """
    keys = [pubkey_bytes(r) for r in recipients]
    return all(keys[i] < keys[i + 1] for i in range(len(keys) - 1))


def batch_preimage(
    *,
    program_id: str,
    life_mint: str,
    bid: int,
    day: int,
    entries: Iterable[tuple[str, int]],
) -> bytes:
    """§4: the 91 + N*40 byte preimage.

    `entries` is an ordered sequence of (recipient_base58, amount_raw) pairs,
    one per WALLET, in the batch's own order.  This function hashes the order
    it is given — the sort contract is verified separately so that an unsorted
    batch produces a *finding*, not a silently re-sorted hash.
    """
    ents = [(str(r), int(a)) for r, a in entries]
    if len(ents) > 255:
        raise ValueError(f"entry_count must fit in a u8, got {len(ents)}")
    for name, val, hi in (("batchId", bid, U64_MAX), ("day", day, U32_MAX)):
        if not isinstance(val, int) or val < 0 or val > hi:
            raise ValueError(f"{name} out of range: {val!r}")

    out = bytearray(DOMAIN_TAG)
    out += pubkey_bytes(program_id)
    out += pubkey_bytes(life_mint)
    out += struct.pack("<Q", bid)
    out += struct.pack("<I", day)
    out += bytes([len(ents)])
    for recipient, amount in ents:
        if not isinstance(amount, int) or amount < 0 or amount > U64_MAX:
            raise ValueError(f"amount out of u64 range: {amount!r}")
        out += pubkey_bytes(recipient)
        out += struct.pack("<Q", amount)

    expected_len = 91 + len(ents) * 40
    if len(out) != expected_len:
        raise AssertionError(
            f"preimage length {len(out)} != 91 + {len(ents)}*40 = {expected_len}")
    return bytes(out)


def batch_hash_bytes(*, program_id: str, life_mint: str, bid: int, day: int,
                     entries: Iterable[tuple[str, int]]) -> bytes:
    """§4: the 32 raw bytes that get signed (§8)."""
    return hashlib.sha256(batch_preimage(
        program_id=program_id, life_mint=life_mint,
        bid=bid, day=day, entries=entries)).digest()


def batch_hash_hex(*, program_id: str, life_mint: str, bid: int, day: int,
                   entries: Iterable[tuple[str, int]]) -> str:
    return batch_hash_bytes(program_id=program_id, life_mint=life_mint,
                            bid=bid, day=day, entries=entries).hex()


def log_hash_hex(canonical_ndjson: bytes) -> str:
    """§9: SHA-256 over the canonical daily log's exact UTF-8 bytes."""
    if not isinstance(canonical_ndjson, (bytes, bytearray)):
        raise TypeError("canonical log must be hashed as raw bytes, not str")
    return hashlib.sha256(bytes(canonical_ndjson)).hexdigest()


def day_to_iso(day: int) -> str:
    """§2: day = floor(unix_ms / 86_400_000) -> the YYYY-MM-DD GET /log key."""
    import datetime as _dt
    return (_dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
            + _dt.timedelta(days=int(day))).strftime("%Y-%m-%d")


def iso_to_day(iso: str) -> int:
    import datetime as _dt
    d = _dt.datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    return int(d.timestamp() * 1000) // 86_400_000
