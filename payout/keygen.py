"""Offline creation and inspection of the payout signing key.

This is the ONLY module in the tree that writes a key file, and it is reached
only through the explicit `python -m payout.cli keygen` command.  The signer
(`signer_key.py`) still never creates or rotates anything.

Guarantees, all enforced here rather than documented:

  * NEVER OVERWRITE.  The file is created with `os.open(..., O_CREAT|O_EXCL)`,
    so an existing path fails at the syscall with EEXIST.  There is no
    "--force"; a second keygen to the same path is always refused.
  * OWNER-ONLY.  The descriptor is `fchmod`-ed to 0600 after creation (the
    `mode=` argument of os.open is masked by the process umask, so it cannot
    be trusted on its own), and the final mode is re-read from the file and
    asserted before the command reports success.
  * SELF-CHECKED.  The new key signs a test message which is verified against
    the derived public key, and the file is read back off disk and decoded
    again; the reloaded public key must equal the in-memory one.  A key that
    fails any of these is UNLINKED and an error is raised, so a half-written
    or non-functional key file is never left behind.
  * SEED NEVER LEAVES.  No function here returns, logs, formats or raises the
    seed or the 64-byte secret.  Only base58/hex public keys and paths do.

Seed source is `os.urandom(32)` — the OS CSPRNG, not a passphrase, not a
derived or reproducible value.  Ed25519 derivation uses `solders.Keypair`,
the exact library `PayoutSigner.sign_batch_hash` signs with, so the public
key printed here is by construction the public key the checker will present.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from solders.keypair import Keypair

from .errors import PayoutKeyError

#: Mode every payout key file must have: readable/writable by the owner only.
KEY_FILE_MODE = 0o600

#: Permission bits that must NOT be set (group or other, read/write/execute).
_FORBIDDEN_BITS = 0o077

#: Message used only for the post-generation sign/verify self-check.
_SELFTEST_MESSAGE = b"payout-keygen self-test: sign and verify"


def require_owner_only(path: str | Path, label: str) -> None:
    """Refuse a key file that any principal other than its owner can read.

    Startup guard: a payout key with group- or world-readable permissions is
    treated as already disclosed.  Mode bits are read with `os.stat` (the
    link is resolved, so a symlink to a loose-permission file is caught too).
    """
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        raise PayoutKeyError(f"{label} file not found: {p}") from None
    except OSError as exc:
        raise PayoutKeyError(f"{label} file cannot be stat'ed: {p} ({exc.strerror})") from None

    bad = stat.S_IMODE(st.st_mode) & _FORBIDDEN_BITS
    if bad:
        raise PayoutKeyError(
            f"{label} file {p} has mode {stat.S_IMODE(st.st_mode):04o}; it is "
            f"readable or writable by someone other than its owner "
            f"(offending bits {bad:04o}).  A payout key with open permissions "
            f"is treated as disclosed — refusing to start.  Fix with: "
            f"chmod 600 {p}")


def read_pubkey(path: str | Path) -> str:
    """Base58 public key stored in a Solana keypair file.  No secret returned.

    Only the trailing 32 bytes (the public half) are turned into the result;
    the leading seed is decoded solely to confirm the file is a consistent
    keypair and is dropped immediately.
    """
    p = Path(path)
    if not p.is_file():
        raise PayoutKeyError(f"key file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError:
        raise PayoutKeyError(f"key file is not valid JSON: {p}") from None
    if not isinstance(raw, list) or len(raw) != 64:
        got = len(raw) if isinstance(raw, list) else type(raw).__name__
        raise PayoutKeyError(
            f"key file must be a JSON array of 64 numbers "
            f"(32 seed || 32 public key), got {got}: {p}")
    if not all(isinstance(b, int) and 0 <= b <= 255 for b in raw):
        raise PayoutKeyError(f"key file contains a value outside 0..255: {p}")
    try:
        kp = Keypair.from_bytes(bytes(raw))
    except Exception:
        raise PayoutKeyError(f"key file does not decode to an Ed25519 keypair: {p}") from None
    pub = str(kp.pubkey())
    del kp, raw
    return pub


def generate_keypair_file(path: str | Path) -> str:
    """Create a new payout key at `path`; return ONLY its base58 public key.

    Refuses to overwrite.  Writes the Solana keypair format (JSON array of 64
    numbers: 32-byte seed followed by the 32-byte public key) through an
    exclusively-created descriptor at mode 0600, then self-checks the result
    and unlinks it if anything is wrong.
    """
    p = Path(path)

    parent = p.parent
    if str(parent) and not parent.is_dir():
        raise PayoutKeyError(
            f"directory for the key file does not exist: {parent} "
            f"(create it first, owner-only: mkdir -p -m 700 {parent})")

    # ── exclusive creation: an existing path fails here, with no write ──────
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    except FileExistsError:
        raise PayoutKeyError(
            f"refusing to overwrite an existing key file: {p}.  keygen never "
            f"replaces a key; move the old file aside deliberately if you "
            f"really mean to rotate") from None
    except OSError as exc:
        raise PayoutKeyError(f"cannot create key file {p}: {exc.strerror}") from None

    created = True
    try:
        # O_CREAT's mode is masked by umask — force the bits we promise.
        os.fchmod(fd, KEY_FILE_MODE)

        seed = os.urandom(32)
        kp = Keypair.from_seed(seed)
        pub_bytes = bytes(kp.pubkey())
        pubkey = str(kp.pubkey())

        payload = json.dumps(list(seed + pub_bytes)).encode("ascii")
        with os.fdopen(fd, "wb") as fh:
            fd = -1  # ownership moved to fh; it closes on exit
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        del seed, payload

        # ── self-check 1: the key actually signs, and the signature verifies ─
        sig = kp.sign_message(_SELFTEST_MESSAGE)
        if not sig.verify(kp.pubkey(), _SELFTEST_MESSAGE):
            raise PayoutKeyError(
                "generated key failed its sign/verify self-test; the file was "
                "removed and no key was created")
        if sig.verify(kp.pubkey(), _SELFTEST_MESSAGE + b"!"):
            raise PayoutKeyError(
                "generated key verified a message it did not sign; the file "
                "was removed and no key was created")
        del kp, sig

        # ── self-check 2: read the file back off disk and re-derive ─────────
        reloaded = read_pubkey(p)
        if reloaded != pubkey:
            raise PayoutKeyError(
                "key file read back from disk does not match the key that was "
                "generated; the file was removed and no key was created")

        # ── self-check 3: the permissions actually landed ───────────────────
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode != KEY_FILE_MODE:
            raise PayoutKeyError(
                f"key file {p} ended up with mode {mode:04o}, not "
                f"{KEY_FILE_MODE:04o}; the file was removed and no key was "
                f"created")
        require_owner_only(p, "new key")

        created = False  # everything passed; keep the file
        return pubkey
    finally:
        if fd >= 0:
            os.close(fd)
        if created:
            try:
                os.unlink(p)
            except OSError:
                pass
