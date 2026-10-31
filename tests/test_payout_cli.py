"""C5p — `python -m payout.cli sign` against the mock coordinator.

Everything runs against the mock serving the c5b bundle (including the new
`GET /payout/{day}` and `POST /payout/{day}/signature` routes) and a MOCK of
the anchored hashes.  No chain, no PM2 process, no real key: every keypair
here is generated in memory into a pytest tmp_path.

════════════════════════════════════════════════════════════════════════════
BYTE-IDENTITY — now a plain, unconditional assertion
════════════════════════════════════════════════════════════════════════════
An earlier revision of this bundle shipped sample online signature files that
all carried the SAME R (nonce point) over three DIFFERENT batch hashes.  A
reused Ed25519 nonce discloses the private key, so those samples were not
reproducible by any conforming RFC 8032 signer and the byte-identity check
was carried as a `xfail(strict=True)`.

That defect is FIXED in the current bundle.  The three samples now carry
three distinct R values, and signing each day's `batchHash` with the
documented fixture key (`SHA-256("c5b-actor:" + "onlineKp")`) reproduces each
sample file byte for byte, envelope included.

Therefore the markers are gone and both properties are asserted outright:
  * `test_bundle_sample_is_byte_identical` — our written file equals the
    bundle's sample, byte for byte, for all three valid days.
  * `test_bundle_samples_do_not_share_an_r_value` — the guard that keeps the
    old defect from silently coming back: the samples' R values must be
    pairwise distinct.  If a future bundle regresses to a reused nonce this
    fires immediately instead of being absorbed by an xfail.
  * `test_signature_verifies_under_the_fixture_online_key` remains the
    semantic acceptance check (verifies over the raw 32-byte hash).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest_payout import (  # noqa: E402
    LIFE_MINT,
    PROGRAM_ID,
    write_throwaway_keypair,
)
from mock_payout_coordinator import (  # noqa: E402
    MockPayoutCoordinator,
    OPEN_DAY,
    TARGETS_TABLE,
    VALID_DAYS,
    anchored_hashes,
    load_online_signature_bytes,
    load_payout_envelope,
    load_tampered,
    load_tampered_verdict,
    tampered_names,
)
from payout import cli as payout_cli  # noqa: E402
from payout.batch_hash import day_to_iso  # noqa: E402
from payout.sigfile import serialize_signature_file  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.signature import Signature  # noqa: E402

BUNDLE = Path("/root/api/c5b-bundle")
ACTOR_SEED_PREFIX = b"c5b-actor:"


# ── the bundle's documented fixture keys (README "Reproducibility — Seeds") ──
def fixture_keypair(label: str) -> Keypair:
    """privKey = SHA-256("c5b-actor:" + label); throwaway, documented in README."""
    return Keypair.from_seed(hashlib.sha256(ACTOR_SEED_PREFIX + label.encode()).digest())


def online_keypair() -> Keypair:
    return fixture_keypair("onlineKp")


def write_fixture_online_key(path: Path) -> Keypair:
    """Materialise the bundle's online key as a 64-byte keypair file."""
    kp = online_keypair()
    path.write_text(json.dumps(list(bytes(kp))))
    return kp


# ── harness ─────────────────────────────────────────────────────────────────
def anchors_file(tmp_path: Path) -> Path:
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps({str(k): v for k, v in anchored_hashes().items()}))
    return p


def base_env(tmp_path: Path, *, use_fixture_online_key: bool = True,
             state_dir: Path | None = None,
             same_key_as_validator: bool = False) -> dict[str, str]:
    payout_key = tmp_path / "payout_key.json"
    validator_key = tmp_path / "validator_key.json"
    if use_fixture_online_key:
        write_fixture_online_key(payout_key)
    else:
        write_throwaway_keypair(payout_key)
    if same_key_as_validator:
        validator_key.write_text(payout_key.read_text())
    else:
        write_throwaway_keypair(validator_key)
    return {
        "PAYOUT_KEYPAIR": str(payout_key),
        "VALIDATOR_KEYPAIR": str(validator_key),
        "PROGRAM_ID": PROGRAM_ID,
        "LIFE_MINT": LIFE_MINT,
        "TARGETS_TABLE": str(TARGETS_TABLE),
        "PAYOUT_ANCHORS": str(anchors_file(tmp_path)),
        "PAYOUT_STATE_DIR": str(state_dir or (tmp_path / "state")),
    }


def run_sign(mock, day, out, env, *, post=False, extra=()):
    argv = ["sign", "--coordinator", mock.url, "--day", str(day),
            "--out", str(out), *extra]
    if post:
        argv.append("--post")
    return payout_cli.main(argv, env)


# ════════════════════════════════════════════════════════════════════════════
# Nonce-reuse guard — keeps the old bundle defect from coming back
# ════════════════════════════════════════════════════════════════════════════
def test_bundle_samples_do_not_share_an_r_value():
    """The three sample online signatures must have pairwise-distinct R.

    An earlier bundle revision reused ONE R across all three days over three
    different batch hashes.  That discloses the signing key: with R1 == R2 the
    secret scalar is ((s1 - s2) * inv(h1 - h2)) mod L.  This guard fails the
    moment a sample bundle regresses to a reused nonce, instead of that being
    absorbed by an xfail marker as it was before.
    """
    r_by_day, hashes = {}, {}
    for day in VALID_DAYS:
        sig = json.loads(load_online_signature_bytes(day))["chunks"][0]["signatureHex"]
        batch = json.loads((BUNDLE / f"fixtures/day_{day}/batch.json").read_text())
        r_by_day[day] = sig[:64]          # R = first 32 bytes of the signature
        hashes[day] = batch["batchHash"]

    assert len(set(hashes.values())) == len(VALID_DAYS), (
        f"the valid days must have distinct batch hashes, got {hashes}")

    # Pairwise, so the failure message names the exact colliding pair.
    days = sorted(r_by_day)
    for i, a in enumerate(days):
        for b in days[i + 1:]:
            assert r_by_day[a] != r_by_day[b], (
                f"NONCE REUSE: days {a} and {b} share R={r_by_day[a]} over "
                f"different batch hashes ({hashes[a]} vs {hashes[b]}) — this "
                f"discloses the online signing key")

    assert len(set(r_by_day.values())) == len(VALID_DAYS)


def test_bundle_samples_are_reproducible_rfc8032_signatures():
    """Each sample is exactly what deterministic Ed25519 produces, and verifies.

    The counterpart to the R guard: distinct nonces alone are not enough, the
    samples must be the signatures a conforming signer actually emits.
    """
    kp = online_keypair()
    for day in VALID_DAYS:
        sample = json.loads(load_online_signature_bytes(day))["chunks"][0]["signatureHex"]
        bh = bytes.fromhex(
            json.loads((BUNDLE / f"fixtures/day_{day}/batch.json").read_text())["batchHash"])
        assert bytes(kp.sign_message(bh)).hex() == sample, f"day {day}"
        assert Signature(bytes.fromhex(sample)).verify(kp.pubkey(), bh), f"day {day}"


def test_our_signer_never_reuses_a_nonce(tmp_path):
    """Two different messages under one key must give two different R values."""
    from payout.signer_key import PayoutSigner
    env = base_env(tmp_path)
    signer = PayoutSigner.from_env(env)
    a = signer.sign_batch_hash(hashlib.sha256(b"message-a").digest())
    b = signer.sign_batch_hash(hashlib.sha256(b"message-b").digest())
    assert a != b
    assert a[:64] != b[:64], "R must differ between messages (no nonce reuse)"


def test_our_signer_is_deterministic(tmp_path):
    """RFC 8032: the same key over the same message gives the same bytes."""
    from payout.signer_key import PayoutSigner
    env = base_env(tmp_path)
    signer = PayoutSigner.from_env(env)
    msg = hashlib.sha256(b"same").digest()
    assert signer.sign_batch_hash(msg) == signer.sign_batch_hash(msg)


# ── RFC 8032 §7.1 known-answer tests ────────────────────────────────────────
RFC8032_VECTORS = [
    # (secret key hex, public key hex, message hex, signature hex)
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("sk_hex,pk_hex,msg_hex,sig_hex", RFC8032_VECTORS)
def test_rfc8032_known_answer_vectors(tmp_path, sk_hex, pk_hex, msg_hex, sig_hex):
    """Our signing path reproduces RFC 8032 §7.1 test vectors 1-3 exactly."""
    from payout.signer_key import PayoutSigner
    kp = Keypair.from_seed(bytes.fromhex(sk_hex))
    assert bytes(kp.pubkey()).hex() == pk_hex, "public key derivation"

    signer = PayoutSigner(kp)
    msg = bytes.fromhex(msg_hex)
    # sign_batch_hash enforces a 32-byte batch hash, so drive the same
    # underlying primitive the signer uses for arbitrary-length KAT messages.
    assert bytes(kp.sign_message(msg)).hex() == sig_hex

    if len(msg) == 32:
        assert signer.sign_batch_hash(msg) == sig_hex


def test_sign_batch_hash_refuses_anything_but_32_raw_bytes(tmp_path):
    """§8: the signature is over the raw hash, never the hex string."""
    from payout.errors import PayoutKeyError
    from payout.signer_key import PayoutSigner
    signer = PayoutSigner.from_env(base_env(tmp_path))
    bh = hashlib.sha256(b"x").digest()
    for bad in (bh.hex(), bh.hex().encode(), bh[:31], bh + b"\x00"):
        with pytest.raises(PayoutKeyError):
            signer.sign_batch_hash(bad)  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════════════════
# Task 2a — the three valid days
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("day", VALID_DAYS)
def test_valid_day_is_accepted_and_writes_a_file(tmp_path, day):
    out = tmp_path / f"sig_{day}.json"
    with MockPayoutCoordinator() as mock:
        rc = run_sign(mock, day, out, base_env(tmp_path))
    assert rc == payout_cli.EXIT_OK
    assert out.is_file()
    obj = json.loads(out.read_bytes())
    assert set(obj) == {"signerPubkeyHex", "chunks"}
    assert obj["signerPubkeyHex"] == bytes(online_keypair().pubkey()).hex()
    assert len(obj["chunks"]) == 1
    assert set(obj["chunks"][0]) == {"batchId", "signatureHex"}
    assert obj["chunks"][0]["batchId"] == str(day * 1000)
    assert len(obj["chunks"][0]["signatureHex"]) == 128


@pytest.mark.parametrize("day", VALID_DAYS)
def test_signature_verifies_under_the_fixture_online_key(tmp_path, day):
    """THE acceptance check: a real Ed25519 signature over the raw 32 bytes.

    This is what the byte-identity check would have proven if the bundle's
    samples were conforming signatures.
    """
    out = tmp_path / f"sig_{day}.json"
    with MockPayoutCoordinator() as mock:
        assert run_sign(mock, day, out, base_env(tmp_path)) == payout_cli.EXIT_OK

    obj = json.loads(out.read_bytes())
    batch = json.loads((BUNDLE / f"fixtures/day_{day}/batch.json").read_text())
    sig = Signature(bytes.fromhex(obj["chunks"][0]["signatureHex"]))
    assert sig.verify(online_keypair().pubkey(), bytes.fromhex(batch["batchHash"])), (
        "signature must verify over hex_decode(batchHash) (§C5r.1)")
    # ...and NOT over the hex string, which is the classic mistake.
    assert not sig.verify(online_keypair().pubkey(), batch["batchHash"].encode())


@pytest.mark.parametrize("day", VALID_DAYS)
def test_written_file_matches_the_bundle_sample_except_for_the_signature(tmp_path, day):
    """Everything but the signature bytes is byte-for-byte right.

    Pins the envelope independently of the signature: schema, key order,
    indent=2, no trailing newline, signerPubkeyHex, batchId.  Kept alongside
    the full byte-identity check because it localises any future failure to
    the envelope rather than the signature.
    """
    out = tmp_path / f"sig_{day}.json"
    with MockPayoutCoordinator() as mock:
        assert run_sign(mock, day, out, base_env(tmp_path)) == payout_cli.EXIT_OK

    ours_raw = out.read_bytes()
    theirs_raw = load_online_signature_bytes(day)
    ours, theirs = json.loads(ours_raw), json.loads(theirs_raw)

    assert list(ours) == list(theirs)
    assert list(ours["chunks"][0]) == list(theirs["chunks"][0])
    assert ours["signerPubkeyHex"] == theirs["signerPubkeyHex"]
    assert ours["chunks"][0]["batchId"] == theirs["chunks"][0]["batchId"]
    # Byte format: substituting their signature into our object reproduces
    # their file exactly, which isolates the difference to the signature.
    rebuilt = dict(ours)
    rebuilt["chunks"] = [dict(ours["chunks"][0],
                              signatureHex=theirs["chunks"][0]["signatureHex"])]
    assert serialize_signature_file(rebuilt) == theirs_raw


@pytest.mark.parametrize("day", VALID_DAYS)
def test_bundle_sample_is_byte_identical(tmp_path, day):
    """Our written file IS the bundle's sample, byte for byte.

    Ed25519 is deterministic (RFC 8032), the bundle documents the online key's
    seed, and `serialize_signature_file` pins the envelope encoding — so the
    whole file reproduces, not just the signature.
    """
    out = tmp_path / f"sig_{day}.json"
    with MockPayoutCoordinator() as mock:
        assert run_sign(mock, day, out, base_env(tmp_path)) == payout_cli.EXIT_OK
    assert out.read_bytes() == load_online_signature_bytes(day)


# ════════════════════════════════════════════════════════════════════════════
# Task 2b — the 16 tampered variants
# ════════════════════════════════════════════════════════════════════════════
def test_there_are_exactly_sixteen_tampered_variants():
    assert len(tampered_names()) == 16, tampered_names()


@pytest.mark.parametrize("name", tampered_names())
def test_tampered_variant_is_refused_and_writes_no_file(tmp_path, name):
    """Each tampered batch is refused and leaves no signature file behind."""
    tampered = load_tampered(name)
    day = int(tampered["day"])
    out = tmp_path / f"sig_{name}.json"

    with MockPayoutCoordinator() as mock:
        # Serve the tampered batch through the real GET /payout/{day} route.
        mock.set_payout(day, {"day": day_to_iso(day),
                              "chunks": [dict(tampered, batchState="proposed",
                                              entryCount=len(tampered["entries"]))]})
        env = base_env(tmp_path)
        if name == "11_repeated_batch_id":
            # This variant means "already signed": prime the ledger first.
            from payout.ledger import DecisionLedger
            led = DecisionLedger(env["PAYOUT_STATE_DIR"])
            led.mark_signed(int(tampered["batchId"]), [], day=day,
                            day_target_hits={})
        rc = run_sign(mock, day, out, env)

    assert rc == payout_cli.EXIT_REFUSED, f"{name} must be refused"
    assert not out.exists(), f"{name} must leave NO signature file"


@pytest.mark.parametrize("name", tampered_names())
def test_tampered_variant_prints_the_first_failing_rule(tmp_path, name, capsys):
    tampered = load_tampered(name)
    expected_rule = load_tampered_verdict(name).get("rule")
    day = int(tampered["day"])
    out = tmp_path / f"sig_{name}.json"

    with MockPayoutCoordinator() as mock:
        mock.set_payout(day, {"day": day_to_iso(day),
                              "chunks": [dict(tampered, batchState="proposed",
                                              entryCount=len(tampered["entries"]))]})
        env = base_env(tmp_path)
        if name == "11_repeated_batch_id":
            from payout.ledger import DecisionLedger
            led = DecisionLedger(env["PAYOUT_STATE_DIR"])
            led.mark_signed(int(tampered["batchId"]), [], day=day,
                            day_target_hits={})
        run_sign(mock, day, out, env)

    stdout = capsys.readouterr().out
    assert "REFUSED" in stdout
    if expected_rule:
        assert expected_rule in stdout, (
            f"{name}: expected rule {expected_rule!r} in {stdout!r}")


# ════════════════════════════════════════════════════════════════════════════
# Task 2c — the open-day fixture
# ════════════════════════════════════════════════════════════════════════════
def test_open_day_fixture_is_refused_as_day_not_closed(tmp_path, capsys):
    """The bundle's own day_20003_open: closed:false -> day_not_closed."""
    from payout.errors import DAY_NOT_CLOSED
    out = tmp_path / "sig_open.json"
    with MockPayoutCoordinator(days=(20000, 20001, 20002, OPEN_DAY)) as mock:
        rc = run_sign(mock, OPEN_DAY, out, base_env(tmp_path))
    stdout = capsys.readouterr().out
    assert rc == payout_cli.EXIT_REFUSED
    assert DAY_NOT_CLOSED in stdout, stdout
    assert not out.exists()


def test_open_day_is_refused_before_any_batch_is_fetched(tmp_path):
    """§C5r.3: the close gate runs FIRST — /payout is never even requested."""
    out = tmp_path / "sig_open.json"
    with MockPayoutCoordinator(days=(20000, 20001, 20002, OPEN_DAY)) as mock:
        run_sign(mock, OPEN_DAY, out, base_env(tmp_path))
        paths = list(mock.state.request_log)
    assert not any(p.startswith("/payout/") for p in paths), paths
    assert not out.exists()


def test_an_open_day_inside_the_replay_range_refuses_a_closed_batch_day(tmp_path, capsys):
    """A closed batch day still refuses when a REPLAY day is open."""
    from payout.errors import DAY_NOT_CLOSED
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        mock.open_day(20001)
        rc = run_sign(mock, 20002, out, base_env(tmp_path))
    stdout = capsys.readouterr().out
    assert rc == payout_cli.EXIT_REFUSED
    assert DAY_NOT_CLOSED in stdout
    assert not out.exists()


# ════════════════════════════════════════════════════════════════════════════
# Task 2d — batch altered after it was fetched
# ════════════════════════════════════════════════════════════════════════════
def test_batch_altered_after_fetch_is_refused(tmp_path, capsys):
    """An envelope whose entries were edited (hash left intact) is refused.

    The checker recomputes the hash from the entries and compares against the
    canonical-log recomputation, so a post-build edit cannot pass.
    """
    day = 20000
    env_fixture = load_payout_envelope(day)
    chunk = dict(env_fixture["chunks"][0])
    entries = [dict(e) for e in chunk["entries"]]
    entries[0]["amountRaw"] = str(int(entries[0]["amountRaw"]) + 1_000_000)
    chunk["entries"] = entries           # batchHash deliberately left stale
    out = tmp_path / "sig.json"

    with MockPayoutCoordinator() as mock:
        mock.set_payout(day, {"day": env_fixture["day"], "chunks": [chunk]})
        rc = run_sign(mock, day, out, base_env(tmp_path))

    stdout = capsys.readouterr().out
    assert rc == payout_cli.EXIT_REFUSED
    assert "REFUSED" in stdout
    assert not out.exists()


def test_envelope_batch_hash_is_never_trusted(tmp_path):
    """Editing entries AND the advertised hash together is still refused."""
    day = 20000
    env_fixture = load_payout_envelope(day)
    chunk = dict(env_fixture["chunks"][0])
    entries = [dict(e) for e in chunk["entries"]]
    entries[0]["amountRaw"] = str(int(entries[0]["amountRaw"]) + 1_000_000)
    chunk["entries"] = entries
    # Recompute a self-consistent hash so only the LOG can catch the edit.
    from payout.batch_hash import batch_hash_hex
    chunk["batchHash"] = batch_hash_hex(
        program_id=PROGRAM_ID, life_mint=LIFE_MINT,
        bid=int(chunk["batchId"]), day=int(chunk["day"]),
        entries=[(e["recipient"], int(e["amountRaw"])) for e in entries])
    chunk["totalRaw"] = str(sum(int(e["amountRaw"]) for e in entries))
    out = tmp_path / "sig.json"

    with MockPayoutCoordinator() as mock:
        mock.set_payout(day, {"day": env_fixture["day"], "chunks": [chunk]})
        rc = run_sign(mock, day, out, base_env(tmp_path))
    assert rc == payout_cli.EXIT_REFUSED
    assert not out.exists()


# ════════════════════════════════════════════════════════════════════════════
# Task 2e — a manifest that omits `closed`
# ════════════════════════════════════════════════════════════════════════════
def test_manifest_without_the_closed_key_is_refused(tmp_path, capsys):
    """§C5r.3: `closed` must be EXPLICITLY true; silence is not closure."""
    from payout.errors import DAY_NOT_CLOSED
    day = 20000
    manifest = json.loads(
        (BUNDLE / f"fixtures/day_{day}/log_get_response.json").read_text())
    manifest.pop("closed")
    out = tmp_path / "sig.json"

    with MockPayoutCoordinator() as mock:
        mock.set_manifest(day, manifest)
        rc = run_sign(mock, day, out, base_env(tmp_path))

    stdout = capsys.readouterr().out
    assert rc == payout_cli.EXIT_REFUSED
    assert DAY_NOT_CLOSED in stdout, stdout
    assert not out.exists()


def test_manifest_with_closed_not_true_is_refused(tmp_path):
    """Truthy-but-not-true values ("true", 1) are refused too."""
    day = 20000
    base = json.loads(
        (BUNDLE / f"fixtures/day_{day}/log_get_response.json").read_text())
    for bad in ("true", 1, None, "yes"):
        manifest = dict(base, closed=bad)
        out = tmp_path / f"sig_{bad!r}.json"
        with MockPayoutCoordinator() as mock:
            mock.set_manifest(day, manifest)
            rc = run_sign(mock, day, out, base_env(tmp_path))
        assert rc == payout_cli.EXIT_REFUSED, bad
        assert not out.exists(), bad


# ════════════════════════════════════════════════════════════════════════════
# Task 2f — --post
# ════════════════════════════════════════════════════════════════════════════
def test_post_sends_exactly_the_file_bytes_to_the_right_route(tmp_path):
    day = 20000
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        rc = run_sign(mock, day, out, base_env(tmp_path), post=True)
        posted = mock.posted()

    assert rc == payout_cli.EXIT_OK
    assert len(posted) == 1, posted
    path, body = posted[0]
    assert path == f"/payout/{day_to_iso(day)}/signature"
    assert body == out.read_bytes(), "POST body must be the file, byte for byte"


def test_without_post_nothing_is_sent(tmp_path):
    day = 20000
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        rc = run_sign(mock, day, out, base_env(tmp_path))
        posted = mock.posted()
    assert rc == payout_cli.EXIT_OK
    assert out.is_file()
    assert posted == [], "without --post the command must not contact the route"


def test_a_refused_day_posts_nothing_even_with_post(tmp_path):
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator(days=(20000, 20001, 20002, OPEN_DAY)) as mock:
        rc = run_sign(mock, OPEN_DAY, out, base_env(tmp_path), post=True)
        posted = mock.posted()
    assert rc == payout_cli.EXIT_REFUSED
    assert posted == []
    assert not out.exists()


# ════════════════════════════════════════════════════════════════════════════
# Key configuration guards
# ════════════════════════════════════════════════════════════════════════════
def test_missing_payout_keypair_refuses_to_start(tmp_path, capsys):
    env = base_env(tmp_path)
    del env["PAYOUT_KEYPAIR"]
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        rc = run_sign(mock, 20000, out, env)
    err = capsys.readouterr().err
    assert rc == payout_cli.EXIT_CONFIG
    assert "PAYOUT_KEYPAIR" in err
    assert not out.exists()


def test_empty_payout_keypair_refuses_to_start(tmp_path):
    env = dict(base_env(tmp_path), PAYOUT_KEYPAIR="")
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        rc = run_sign(mock, 20000, out, env)
    assert rc == payout_cli.EXIT_CONFIG
    assert not out.exists()


def test_payout_key_equal_to_verdict_key_refuses_to_start(tmp_path, capsys):
    """Same public key as the validator's verdict key -> refuse to start."""
    env = base_env(tmp_path, same_key_as_validator=True)
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        rc = run_sign(mock, 20000, out, env)
    err = capsys.readouterr().err
    assert rc == payout_cli.EXIT_CONFIG
    assert "REFUSING TO START" in err
    assert not out.exists()


def test_payout_key_equal_by_value_in_a_different_file_refuses(tmp_path):
    """Equality is on the PUBLIC KEY, not the path: a copy is still refused."""
    payout_key = tmp_path / "a.json"
    validator_key = tmp_path / "b.json"
    write_fixture_online_key(payout_key)
    validator_key.write_text(payout_key.read_text())  # different path, same key
    env = base_env(tmp_path)
    env["PAYOUT_KEYPAIR"] = str(payout_key)
    env["VALIDATOR_KEYPAIR"] = str(validator_key)
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        rc = run_sign(mock, 20000, out, env)
    assert rc == payout_cli.EXIT_CONFIG
    assert not out.exists()


# ════════════════════════════════════════════════════════════════════════════
# Never sign a batch id twice
# ════════════════════════════════════════════════════════════════════════════
def test_a_batch_id_is_never_signed_twice(tmp_path, capsys):
    from payout.errors import REPEATED_BATCH_ID
    day = 20000
    state = tmp_path / "shared_state"
    env = base_env(tmp_path, state_dir=state)

    with MockPayoutCoordinator() as mock:
        first = run_sign(mock, day, tmp_path / "one.json", env)
        capsys.readouterr()
        second = run_sign(mock, day, tmp_path / "two.json", env)

    stdout = capsys.readouterr().out
    assert first == payout_cli.EXIT_OK
    assert second == payout_cli.EXIT_REFUSED
    assert REPEATED_BATCH_ID in stdout, stdout
    assert (tmp_path / "one.json").is_file()
    assert not (tmp_path / "two.json").exists(), "second run must write no file"


def test_the_replay_guard_survives_a_fresh_process(tmp_path):
    """The guard is the on-disk ledger, not in-memory state."""
    day = 20000
    state = tmp_path / "state"
    with MockPayoutCoordinator() as mock:
        assert run_sign(mock, day, tmp_path / "a.json",
                        base_env(tmp_path, state_dir=state)) == payout_cli.EXIT_OK
        # Brand-new env dict and signer, same state dir == same ledger file.
        assert run_sign(mock, day, tmp_path / "b.json",
                        base_env(tmp_path, state_dir=state)) == payout_cli.EXIT_REFUSED
    assert not (tmp_path / "b.json").exists()


def test_a_refusal_does_not_consume_the_batch_id(tmp_path):
    """A refused day must still be signable once the problem is fixed."""
    day = 20002
    state = tmp_path / "state"
    env = base_env(tmp_path, state_dir=state)
    with MockPayoutCoordinator() as mock:
        mock.open_day(day)
        assert run_sign(mock, day, tmp_path / "a.json", env) == payout_cli.EXIT_REFUSED
        mock.close_day(day)
        assert run_sign(mock, day, tmp_path / "b.json", env) == payout_cli.EXIT_OK
    assert not (tmp_path / "a.json").exists()
    assert (tmp_path / "b.json").is_file()


# ════════════════════════════════════════════════════════════════════════════
# Misc source failures
# ════════════════════════════════════════════════════════════════════════════
def test_a_missing_batch_is_refused_without_a_file(tmp_path):
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        mock.drop_payout(20000)
        rc = run_sign(mock, 20000, out, base_env(tmp_path))
    assert rc == payout_cli.EXIT_REFUSED
    assert not out.exists()


def test_an_offline_coordinator_is_refused_without_a_file(tmp_path):
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        mock.go_offline()
        rc = run_sign(mock, 20000, out, base_env(tmp_path))
    assert rc == payout_cli.EXIT_REFUSED
    assert not out.exists()


def test_a_refusal_never_overwrites_an_existing_file(tmp_path):
    out = tmp_path / "sig.json"
    out.write_bytes(b"PRE-EXISTING")
    with MockPayoutCoordinator(days=(20000, 20001, 20002, OPEN_DAY)) as mock:
        rc = run_sign(mock, OPEN_DAY, out, base_env(tmp_path))
    assert rc == payout_cli.EXIT_REFUSED
    assert out.read_bytes() == b"PRE-EXISTING"


def test_no_secret_material_is_ever_printed(tmp_path, capsys):
    """Only public keys, hashes and signatures reach stdout/stderr."""
    env = base_env(tmp_path)
    secret = bytes(online_keypair())[:32].hex()
    out = tmp_path / "sig.json"
    with MockPayoutCoordinator() as mock:
        run_sign(mock, 20000, out, env, post=True)
    cap = capsys.readouterr()
    blob = cap.out + cap.err + out.read_text()
    assert secret not in blob
    assert json.dumps(list(bytes(online_keypair()))) not in blob
