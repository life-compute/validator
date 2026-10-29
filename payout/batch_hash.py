"""Canonical batch id / batch hash (BATCH_SPEC.md §3, §4, §8, §9).

    batchId   = day * 1000 + seq                                      (§3)
    preimage  = batchId_le8 || day_le4 || totalRaw_le8   (20 bytes)   (§4)
    batchHash = SHA-256(preimage)                                     (§4)
    logHash   = SHA-256(canonical_daily_log_ndjson_utf8)              (§9)

`totalRaw` is the sum of MINER amounts only; validator fees are excluded from
the hash preimage and from the on-chain batch total (§4, §6.3).

Signers sign the 32 RAW bytes of batchHash, not the hex string (§8).
"""
from __future__ import annotations

import hashlib
import struct

from .constants import BATCH_ID_DAY_MULTIPLIER

U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1


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


def batch_preimage(bid: int, day: int, total_raw: int) -> bytes:
    """§4: 20-byte little-endian preimage."""
    for name, val, hi in (("batchId", bid, U64_MAX), ("day", day, U32_MAX),
                          ("totalRaw", total_raw, U64_MAX)):
        if not isinstance(val, int) or val < 0 or val > hi:
            raise ValueError(f"{name} out of range: {val!r}")
    return struct.pack("<QIQ", bid, day, total_raw)


def batch_hash_bytes(bid: int, day: int, total_raw: int) -> bytes:
    """§4: the 32 raw bytes that get signed (§8)."""
    return hashlib.sha256(batch_preimage(bid, day, total_raw)).digest()


def batch_hash_hex(bid: int, day: int, total_raw: int) -> str:
    return batch_hash_bytes(bid, day, total_raw).hex()


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
