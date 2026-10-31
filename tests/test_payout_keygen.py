"""C5p3 — `python -m payout.cli keygen` / `pubkey`, and the 0600 start-up guard.

Every key here is a THROWAWAY generated into a pytest `tmp_path`.  No real
wallet path is read or written, no chain, no PM2 process, no coordinator.

The point of this suite is not that keygen produces *a* file — it is that the
file it produces is the right shape, has the right permissions, is usable for
real batch signing, is never silently replaced, and that the seed never
reaches any output channel.  The leak checks are therefore done in all three
encodings the seed could plausibly be printed in (hex, base58, and the
decimal array that is literally how it is stored on disk) and against BOTH
captured streams plus the file the command reports.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import base58
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest_payout import (  # noqa: E402
    LIFE_MINT,
    PROGRAM_ID,
    write_throwaway_keypair,
)
from payout import cli as payout_cli  # noqa: E402
from payout.batch_hash import batch_hash_bytes  # noqa: E402
from payout.errors import PayoutKeyError  # noqa: E402
from payout.keygen import (  # noqa: E402
    KEY_FILE_MODE,
    generate_keypair_file,
    read_pubkey,
    require_owner_only,
)
from payout.signer_key import PayoutSigner  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.signature import Signature  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────
def run_cli(argv: list[str]) -> int:
    """Invoke the CLI exactly as `python -m payout.cli ...` would."""
    return payout_cli.main(argv)


def file_mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def key_bytes(p: Path) -> bytes:
    """The raw 64 bytes on disk.  Used ONLY to build leak-detection needles."""
    return bytes(json.loads(p.read_text()))


def seed_encodings(p: Path) -> dict[str, str]:
    """Every textual form the 32-byte seed could leak in."""
    raw = key_bytes(p)
    seed = raw[:32]
    return {
        "hex": seed.hex(),
        "hex_upper": seed.hex().upper(),
        "base58": base58.b58encode(seed).decode(),
        "base58_full64": base58.b58encode(raw).decode(),
        "decimal_array": json.dumps(list(seed)),
        "decimal_array_spaced": str(list(seed)),
        "first_8_decimals": ",".join(str(b) for b in seed[:8]),
    }


def assert_no_seed_in(text: str, p: Path, where: str) -> None:
    for name, needle in seed_encodings(p).items():
        assert needle not in text, f"seed leaked as {name} into {where}"


# ── 1. the file: 64 numbers, mode 0600 ──────────────────────────────────────
def test_keygen_writes_64_numbers_at_mode_0600(tmp_path, capsys):
    out = tmp_path / "payout-online.json"

    assert run_cli(["keygen", "--out", str(out)]) == 0
    capsys.readouterr()

    assert out.is_file()
    raw = json.loads(out.read_text())
    assert isinstance(raw, list), "Solana keypair format is a JSON array"
    assert len(raw) == 64, f"expected 64 numbers (32 seed || 32 pubkey), got {len(raw)}"
    assert all(isinstance(b, int) and 0 <= b <= 255 for b in raw), \
        "every element must be a byte value 0..255"

    assert file_mode(out) == KEY_FILE_MODE == 0o600, \
        f"key file mode is {file_mode(out):04o}, must be 0600"


def test_the_last_32_bytes_are_the_public_key_of_the_first_32(tmp_path, capsys):
    """Solana format is seed || pubkey — prove the halves actually correspond."""
    out = tmp_path / "k.json"
    assert run_cli(["keygen", "--out", str(out)]) == 0
    capsys.readouterr()

    raw = key_bytes(out)
    derived = Keypair.from_seed(raw[:32]).pubkey()
    assert bytes(derived) == raw[32:], \
        "the trailing 32 bytes are not the public key derived from the seed"


def test_two_keygens_produce_different_keys(tmp_path, capsys):
    """os.urandom(32), not a fixed or derived seed."""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert run_cli(["keygen", "--out", str(a)]) == 0
    assert run_cli(["keygen", "--out", str(b)]) == 0
    capsys.readouterr()
    assert read_pubkey(a) != read_pubkey(b)
    assert key_bytes(a)[:32] != key_bytes(b)[:32]


# ── 2. printed pubkey == what `pubkey` prints ───────────────────────────────
def test_printed_pubkey_equals_what_pubkey_command_prints(tmp_path, capsys):
    out = tmp_path / "k.json"

    assert run_cli(["keygen", "--out", str(out)]) == 0
    gen = capsys.readouterr()

    printed = [ln for ln in gen.out.splitlines() if ln.startswith("pubkey:")]
    assert len(printed) == 1, f"keygen must print exactly one pubkey line, got {gen.out!r}"
    keygen_pub = printed[0].split("pubkey:", 1)[1].strip()

    assert str(out) in gen.out, "keygen must print the path it created"

    assert run_cli(["pubkey", "--key", str(out)]) == 0
    sub = capsys.readouterr()
    pubkey_pub = sub.out.strip()

    assert pubkey_pub == keygen_pub, \
        f"keygen printed {keygen_pub!r} but pubkey printed {pubkey_pub!r}"

    # and both equal the key the SIGNER would present
    assert keygen_pub == str(Keypair.from_bytes(key_bytes(out)).pubkey())

    # base58, decodes to exactly 32 bytes
    assert len(base58.b58decode(keygen_pub)) == 32


def test_pubkey_prints_the_public_key_and_nothing_else(tmp_path, capsys):
    out = tmp_path / "k.json"
    assert run_cli(["keygen", "--out", str(out)]) == 0
    capsys.readouterr()

    assert run_cli(["pubkey", "--key", str(out)]) == 0
    cap = capsys.readouterr()

    assert cap.out.strip().splitlines() == [read_pubkey(out)], \
        f"pubkey must print the key alone, got {cap.out!r}"
    assert cap.err == "", f"pubkey must print nothing to stderr, got {cap.err!r}"


# ── 3. a second keygen to the same path is refused ──────────────────────────
def test_second_keygen_to_the_same_path_is_refused(tmp_path, capsys):
    out = tmp_path / "k.json"

    assert run_cli(["keygen", "--out", str(out)]) == 0
    capsys.readouterr()
    before = out.read_bytes()
    before_mode = file_mode(out)

    rc = run_cli(["keygen", "--out", str(out)])
    cap = capsys.readouterr()

    assert rc != 0, "overwriting an existing key file must not succeed"
    assert out.read_bytes() == before, "the existing key file was modified!"
    assert file_mode(out) == before_mode
    assert "refus" in cap.err.lower(), f"expected a refusal on stderr, got {cap.err!r}"
    assert_no_seed_in(cap.out + cap.err, out, "the refusal output")


def test_keygen_refuses_even_an_empty_existing_file(tmp_path, capsys):
    """O_EXCL, not a content check: any existing path is refused."""
    out = tmp_path / "k.json"
    out.touch()
    rc = run_cli(["keygen", "--out", str(out)])
    capsys.readouterr()
    assert rc != 0
    assert out.read_bytes() == b"", "an existing file must be left untouched"


def test_keygen_refuses_a_missing_parent_directory(tmp_path, capsys):
    out = tmp_path / "nope" / "k.json"
    rc = run_cli(["keygen", "--out", str(out)])
    cap = capsys.readouterr()
    assert rc != 0
    assert not out.exists()
    assert not out.parent.exists(), "keygen must not create the parent directory"
    assert "director" in cap.err.lower()


# ── 4. the key signs a sample batch and the signature verifies ──────────────
def test_the_generated_key_signs_a_sample_batch_and_verifies(tmp_path, capsys):
    """End-to-end: keygen -> PayoutSigner -> sign_batch_hash -> Ed25519 verify."""
    payout_key = tmp_path / "payout.json"
    validator_key = tmp_path / "validator.json"
    assert run_cli(["keygen", "--out", str(payout_key)]) == 0
    capsys.readouterr()
    write_throwaway_keypair(validator_key)

    signer = PayoutSigner.from_env({
        "PAYOUT_KEYPAIR": str(payout_key),
        "VALIDATOR_KEYPAIR": str(validator_key),
    })

    # a real §8 batch hash over a sample batch, not an arbitrary blob
    batch_hash = batch_hash_bytes(
        program_id=PROGRAM_ID,
        life_mint=LIFE_MINT,
        bid=4242,
        day=20002,
        entries=[
            ("11111111111111111111111111111112", 1_000_000),
            ("SysvarRent111111111111111111111111111111111", 2_500_000),
        ],
    )
    assert len(batch_hash) == 32, "§8 signs the 32 RAW bytes of the batch hash"

    sig_hex = signer.sign_batch_hash(batch_hash)
    assert len(sig_hex) == 128, "a §8 signature is 64 bytes = 128 hex chars"

    pub = Keypair.from_bytes(key_bytes(payout_key)).pubkey()
    assert Signature.from_bytes(bytes.fromhex(sig_hex)).verify(pub, batch_hash), \
        "the signature produced by the generated key did not verify"

    # negative control: it must NOT verify a different hash
    other = bytes(32)
    assert not Signature.from_bytes(bytes.fromhex(sig_hex)).verify(pub, other)

    # and the signer's advertised pubkey is the generated one
    assert signer.pubkey == read_pubkey(payout_key)
    assert signer.pubkey_hex == bytes(pub).hex()


def test_a_different_generated_key_produces_a_different_signature(tmp_path,
                                                                  capsys):
    """Two keygens must not be able to sign for each other."""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    validator_key = tmp_path / "validator.json"
    assert run_cli(["keygen", "--out", str(a)]) == 0
    assert run_cli(["keygen", "--out", str(b)]) == 0
    capsys.readouterr()
    write_throwaway_keypair(validator_key)

    batch_hash = batch_hash_bytes(
        program_id=PROGRAM_ID, life_mint=LIFE_MINT, bid=7, day=20002,
        entries=[("11111111111111111111111111111112", 5_000)])

    sigs = {}
    for p in (a, b):
        s = PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(p),
                                   "VALIDATOR_KEYPAIR": str(validator_key)})
        sigs[p.name] = s.sign_batch_hash(batch_hash)

    assert sigs["a.json"] != sigs["b.json"]

    pub_a = Keypair.from_bytes(key_bytes(a)).pubkey()
    assert not Signature.from_bytes(bytes.fromhex(sigs["b.json"])).verify(
        pub_a, batch_hash), "key b's signature verified under key a's pubkey"


# ── 5. the key differs, BY VALUE, from the validator's verdict key ──────────
def test_generated_key_differs_by_value_from_the_validator_verdict_key(tmp_path,
                                                                       capsys):
    payout_key = tmp_path / "payout.json"
    validator_key = tmp_path / "validator.json"

    assert run_cli(["keygen", "--out", str(payout_key)]) == 0
    capsys.readouterr()
    validator_pub = write_throwaway_keypair(validator_key)

    payout_raw = key_bytes(payout_key)
    validator_raw = key_bytes(validator_key)

    assert read_pubkey(payout_key) != validator_pub, "public keys collide"
    assert payout_raw != validator_raw, "key files are byte-identical"
    assert payout_raw[:32] != validator_raw[:32], "the two keys share a seed!"

    # different PATHS alone are not enough — the guard must compare values.
    signer = PayoutSigner.from_env({
        "PAYOUT_KEYPAIR": str(payout_key),
        "VALIDATOR_KEYPAIR": str(validator_key),
    })
    assert signer.pubkey != validator_pub


def test_a_payout_key_equal_to_the_verdict_key_is_still_refused(tmp_path, capsys):
    """Copying the generated key to a second path must not defeat the guard."""
    payout_key = tmp_path / "payout.json"
    validator_key = tmp_path / "validator.json"

    assert run_cli(["keygen", "--out", str(payout_key)]) == 0
    capsys.readouterr()

    validator_key.write_text(payout_key.read_text())  # same VALUE, other path
    validator_key.chmod(0o600)

    with pytest.raises(PayoutKeyError) as exc:
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(payout_key),
                               "VALIDATOR_KEYPAIR": str(validator_key)})
    assert "identical" in str(exc.value)
    assert_no_seed_in(str(exc.value), payout_key, "the refusal message")


# ── 6. nothing anywhere prints the seed ─────────────────────────────────────
def test_no_output_stream_contains_the_seed_in_any_encoding(tmp_path, capsys):
    """stdout and stderr of keygen AND pubkey, in hex, base58 and decimal."""
    out = tmp_path / "k.json"

    assert run_cli(["keygen", "--out", str(out)]) == 0
    gen = capsys.readouterr()

    assert run_cli(["pubkey", "--key", str(out)]) == 0
    sub = capsys.readouterr()

    for stream_name, text in [("keygen stdout", gen.out), ("keygen stderr", gen.err),
                              ("pubkey stdout", sub.out), ("pubkey stderr", sub.err)]:
        assert_no_seed_in(text, out, stream_name)

    # the public key IS expected on stdout — proves the needles aren't vacuous
    pub = read_pubkey(out)
    assert pub in gen.out and pub in sub.out


def test_the_seed_needles_actually_detect_a_leak(tmp_path, capsys):
    """Control: assert_no_seed_in must FAIL on text that does contain a seed."""
    out = tmp_path / "k.json"
    assert run_cli(["keygen", "--out", str(out)]) == 0
    capsys.readouterr()

    for name, needle in seed_encodings(out).items():
        with pytest.raises(AssertionError):
            assert_no_seed_in(f"oops {needle} oops", out, f"control/{name}")


def test_repr_and_str_of_the_signer_hide_the_secret(tmp_path, capsys):
    payout_key = tmp_path / "payout.json"
    validator_key = tmp_path / "validator.json"
    assert run_cli(["keygen", "--out", str(payout_key)]) == 0
    capsys.readouterr()
    write_throwaway_keypair(validator_key)

    signer = PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(payout_key),
                                    "VALIDATOR_KEYPAIR": str(validator_key)})
    for text in (repr(signer), str(signer), f"{signer}", f"{signer!r}"):
        assert_no_seed_in(text, payout_key, "signer repr/str")


def test_keygen_failure_output_never_contains_a_seed(tmp_path, capsys):
    """The refusal path prints paths and reasons — never key material."""
    out = tmp_path / "k.json"
    assert run_cli(["keygen", "--out", str(out)]) == 0
    capsys.readouterr()

    assert run_cli(["keygen", "--out", str(out)]) != 0
    dup = capsys.readouterr()
    assert_no_seed_in(dup.out + dup.err, out, "duplicate-keygen output")

    out.chmod(0o644)
    with pytest.raises(PayoutKeyError) as exc:
        require_owner_only(out, "PAYOUT_KEYPAIR")
    assert_no_seed_in(str(exc.value), out, "permission refusal")
    out.chmod(0o600)


def test_generate_keypair_file_returns_only_a_public_key(tmp_path):
    """The library entry point itself must not hand back secret material."""
    out = tmp_path / "k.json"
    returned = generate_keypair_file(out)
    assert returned == read_pubkey(out)
    assert_no_seed_in(returned, out, "generate_keypair_file return value")
    assert len(base58.b58decode(returned)) == 32


# ── 7. an open-permission key file is refused at start-up ───────────────────
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o660,
                                  0o606, 0o664, 0o777, 0o700 | 0o044])
def test_a_group_or_world_readable_key_is_refused_at_startup(tmp_path, mode,
                                                             capsys):
    payout_key = tmp_path / "payout.json"
    validator_key = tmp_path / "validator.json"
    assert run_cli(["keygen", "--out", str(payout_key)]) == 0
    capsys.readouterr()
    write_throwaway_keypair(validator_key)

    payout_key.chmod(mode)

    with pytest.raises(PayoutKeyError) as exc:
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(payout_key),
                               "VALIDATOR_KEYPAIR": str(validator_key)})

    msg = str(exc.value)
    assert f"{mode:04o}" in msg, f"the refusal must name the offending mode: {msg}"
    assert "owner" in msg.lower()
    assert_no_seed_in(msg, payout_key, "startup permission refusal")

    # the exact same key at 0600 starts fine — the mode is the only difference
    payout_key.chmod(0o600)
    signer = PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(payout_key),
                                    "VALIDATOR_KEYPAIR": str(validator_key)})
    assert signer.pubkey == read_pubkey(payout_key)


def test_the_permission_guard_runs_before_the_file_is_parsed(tmp_path, capsys):
    """A loose-permission file is refused on MODE, not on contents."""
    payout_key = tmp_path / "payout.json"
    validator_key = tmp_path / "validator.json"
    payout_key.write_text("this is not even JSON")
    payout_key.chmod(0o644)
    write_throwaway_keypair(validator_key)

    with pytest.raises(PayoutKeyError) as exc:
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(payout_key),
                               "VALIDATOR_KEYPAIR": str(validator_key)})
    assert "owner" in str(exc.value).lower(), \
        f"expected the permission refusal first, got: {exc.value}"


def test_a_0600_key_passes_the_guard(tmp_path, capsys):
    out = tmp_path / "k.json"
    assert run_cli(["keygen", "--out", str(out)]) == 0
    capsys.readouterr()
    require_owner_only(out, "PAYOUT_KEYPAIR")  # must not raise


def test_the_guard_follows_symlinks(tmp_path, capsys):
    """A tight-looking symlink to a loose file must still be refused."""
    real = tmp_path / "real.json"
    assert run_cli(["keygen", "--out", str(real)]) == 0
    capsys.readouterr()
    real.chmod(0o644)

    link = tmp_path / "link.json"
    link.symlink_to(real)

    with pytest.raises(PayoutKeyError):
        require_owner_only(link, "PAYOUT_KEYPAIR")


def test_the_guard_reports_a_missing_file_clearly(tmp_path):
    with pytest.raises(PayoutKeyError) as exc:
        require_owner_only(tmp_path / "absent.json", "PAYOUT_KEYPAIR")
    assert "not found" in str(exc.value)


# ── 8. keygen honours the umask rather than inheriting it ───────────────────
def test_mode_is_0600_even_under_a_permissive_umask(tmp_path, capsys):
    """os.open's mode argument is masked by umask; fchmod must fix that."""
    old = os.umask(0o000)
    try:
        out = tmp_path / "k.json"
        assert run_cli(["keygen", "--out", str(out)]) == 0
        capsys.readouterr()
        assert file_mode(out) == 0o600, \
            f"umask 000 leaked into the key file: mode {file_mode(out):04o}"
    finally:
        os.umask(old)
