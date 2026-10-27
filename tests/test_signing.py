"""Signature round-trip and key-hygiene tests.

Every key here is a throwaway generated in memory.  Nothing is written to
disk, nothing is rotated, no real wallet file is touched.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

import base58
import pytest

from coordinator_client.signing import (
    EMPTY_BODY_SHA256, Signer, build_message, canonical_body, verify,
)


# ── canonical message shape (contract §1) ───────────────────────────────────

def test_empty_body_hash_matches_contract():
    assert EMPTY_BODY_SHA256 == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_canonical_message_is_five_newline_fields():
    msg = build_message("GET", "/results/pending", b"", "ab" * 16, 1721000000)
    parts = msg.decode().split("\n")
    assert len(parts) == 5
    assert parts[0] == "GET"
    assert parts[1] == "/results/pending"
    assert parts[2] == EMPTY_BODY_SHA256
    assert parts[3] == "ab" * 16
    assert parts[4] == "1721000000"
    assert not msg.endswith(b"\n"), "no trailing newline"


def test_body_bytes_are_compact_json():
    body = canonical_body({"result_id": 42, "validator_score": -8.55})
    assert body == b'{"result_id":42,"validator_score":-8.55}'
    assert canonical_body(None) == b""


# ── round trip ──────────────────────────────────────────────────────────────

def test_signature_round_trip_get(throwaway_signer):
    s = throwaway_signer
    hdr = json.loads(s.auth_header("GET", "/results/pending", b""))
    msg = build_message("GET", "/results/pending", b"",
                        hdr["nonce"], hdr["timestamp"])
    assert verify(hdr["wallet"], msg, hdr["signature"])


def test_signature_round_trip_post(throwaway_signer):
    s = throwaway_signer
    body = canonical_body({"result_id": 7, "validator_score": -8.0})
    hdr = json.loads(s.auth_header("POST", "/verdicts", body))
    msg = build_message("POST", "/verdicts", body, hdr["nonce"], hdr["timestamp"])
    assert verify(hdr["wallet"], msg, hdr["signature"])


def test_signature_rejects_tampered_body(throwaway_signer):
    s = throwaway_signer
    body = canonical_body({"result_id": 7, "validator_score": -8.0})
    hdr = json.loads(s.auth_header("POST", "/verdicts", body))
    tampered = canonical_body({"result_id": 7, "validator_score": -99.0})
    msg = build_message("POST", "/verdicts", tampered, hdr["nonce"], hdr["timestamp"])
    assert not verify(hdr["wallet"], msg, hdr["signature"])


def test_signature_rejects_other_wallet(throwaway_signer):
    other = Signer.generate()
    hdr = json.loads(throwaway_signer.auth_header("GET", "/results/pending", b""))
    msg = build_message("GET", "/results/pending", b"", hdr["nonce"], hdr["timestamp"])
    assert not verify(other.wallet, msg, hdr["signature"])


# ── header field shapes ─────────────────────────────────────────────────────

def test_auth_header_fields(throwaway_signer):
    hdr = json.loads(throwaway_signer.auth_header("GET", "/results/pending", b""))
    assert set(hdr) >= {"wallet", "nonce", "timestamp", "signature"}
    assert len(base58.b58decode(hdr["wallet"])) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", hdr["nonce"]), "nonce = 32 lowercase hex"
    assert re.fullmatch(r"[0-9a-f]{128}", hdr["signature"]), "sig = 128 lowercase hex"
    assert isinstance(hdr["timestamp"], int)
    assert abs(hdr["timestamp"] - int(time.time())) < 5
    assert hdr["worker_name"] == "test-worker"


def test_nonce_is_fresh_per_request(throwaway_signer):
    nonces = {json.loads(throwaway_signer.auth_header("GET", "/x", b""))["nonce"]
              for _ in range(50)}
    assert len(nonces) == 50, "nonce must not repeat within the window"


def test_keypair_file_must_be_64_bytes(tmp_path):
    bad = tmp_path / "short.json"
    bad.write_text(json.dumps([0] * 32))
    with pytest.raises(ValueError, match="64 bytes"):
        Signer.from_keypair_file(bad)


# ── NEGATIVE CONTROL: no key material may ever surface ──────────────────────

def test_signer_repr_does_not_leak_secret(throwaway_signer):
    s = throwaway_signer
    secret_hex = bytes(s._kp.to_bytes_array()[:32]).hex()  # noqa: SLF001 — test probe
    for rendered in (repr(s), str(s), f"{s}", f"{s!r}"):
        assert secret_hex not in rendered
        assert "secret withheld" in rendered


def test_only_public_key_is_exposed(throwaway_signer):
    s = throwaway_signer
    secret = bytes(s._kp.to_bytes_array()[:32])  # noqa: SLF001 — test probe
    assert s.pubkey_bytes != secret
    assert base58.b58decode(s.wallet) == s.pubkey_bytes


def test_signature_is_not_the_key(throwaway_signer):
    s = throwaway_signer
    secret_hex = bytes(s._kp.to_bytes_array()[:32]).hex()  # noqa: SLF001
    hdr = json.loads(s.auth_header("GET", "/results/pending", b""))
    assert secret_hex not in json.dumps(hdr)
