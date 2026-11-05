"""C47 §17.2 — the verdict body carries the identity of the re-scored result.

Three things are proved here and nothing else is assumed:

  1. The four new fields (`molecule_hash`, `target`, `miner_wallet`,
     `claimed_score`) are the PENDING ITEM's values, byte for byte.
  2. A mock Worker that cross-checks them answers 422 on a mismatch, and the
     client surfaces that 422 instead of recording a verdict.
  3. THE BODY THAT IS SIGNED IS THE BODY THAT IS SENT: the mock records the
     exact raw bytes it verified the signature over, and those bytes are
     re-verified here against the validator's public key with an independent
     Ed25519 check, then parsed and compared field by field.

The signing logic itself is UNCHANGED — `Signer.auth_header` is untouched —
so (3) is really a statement about `canonical_body`: the hash in X-Auth is
taken over exactly the bytes urllib puts on the wire.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import base58
import pytest
from solders.pubkey import Pubkey
from solders.signature import Signature

from coordinator_client import contract
from coordinator_client.bias import BiasTable
from coordinator_client.client import CoordinatorClient, ValidatorService
from coordinator_client.progress import ProgressStore
from coordinator_client.rescore import Rescorer
from coordinator_client.signing import Signer
from conftest import FakeRunner, pending_row
from mock_coordinator import MockCoordinator



@pytest.fixture
def coord():
    with MockCoordinator() as c:
        yield c


def _service(coord, tmp_path: Path, msa_dir: Path, *, pred: float = -0.4870):
    signer = Signer.generate("test-worker")
    coord.state.open_validator_registration(signer.wallet)
    client = CoordinatorClient(coord.url, signer, timeout=5,
                               max_attempts=2, backoff_base=0.01,
                               backoff_cap=0.02)
    rescorer = Rescorer(FakeRunner({"affinity_pred_value": pred}),
                        msa_dir=msa_dir, seed=68, num_subsampled_msa=1024,
                        timeout_s=60.0)
    svc = ValidatorService(client, rescorer,
                           ProgressStore(tmp_path / "p.sqlite3"),
                           BiasTable({}),
                           coord_config=client.get_config(),
                           gpu_model="RTX 5060",
                           verdict_log=tmp_path / "verdicts.jsonl")
    return svc, signer


# ── 1. the body carries the pending item's own values ───────────────────────
def test_verdict_body_builder_adds_the_four_identity_fields():
    body = contract.verdict_body(
        7, -8.5, "RTX 5060", "confirm",
        molecule_hash="e72cdc34a39d30c2eb908269e61263b7",
        target="ESR1",
        miner_wallet="4K5sRsNJYvzXjUy9k5MceVgZo8CBUjnMzv44Q2sdPFGY",
        claimed_score=-6.864)
    assert body["molecule_hash"] == "e72cdc34a39d30c2eb908269e61263b7"
    assert body["target"] == "ESR1"
    assert body["miner_wallet"] == "4K5sRsNJYvzXjUy9k5MceVgZo8CBUjnMzv44Q2sdPFGY"
    assert body["claimed_score"] == -6.864
    # The pre-C47 fields are untouched.
    assert body["result_id"] == 7
    assert body["validator_score"] == -8.5
    assert body["validator_verdict"] == "confirm"


def test_omitted_identity_fields_are_simply_absent():
    """Below LOG_V2_FROM_DAY the fields are optional; the builder must not
    invent them (a `null` would be a value the Worker has to interpret)."""
    body = contract.verdict_body(7, -8.5, None, None)
    for field in ("molecule_hash", "target", "miner_wallet", "claimed_score"):
        assert field not in body


def test_posted_verdict_carries_the_pending_rows_values(coord, tmp_path, msa_dir,
                                                        protein_pins):
    row = pending_row(501, score=-8.85, target="CDK4",
                      wallet="4bXgZ1111111111111111111111111111111111111",
                      **protein_pins)
    coord.state.add_pending(row)
    svc, _ = _service(coord, tmp_path, msa_dir)
    stats = svc.process_batch()
    assert stats["verdicts"] == 1, stats

    posted = [r for r in coord.state.signed_requests if r["path"] == "/verdicts"]
    assert len(posted) == 1
    body = json.loads(posted[0]["body"])
    assert body["molecule_hash"] == row["molecule_hash"]
    assert body["target"] == row["target"]
    assert body["miner_wallet"] == row["wallet"]
    assert body["claimed_score"] == row["score"]
    assert body["result_id"] == row["id"]


# ── 2. the Worker rejects a mismatch with 422 ───────────────────────────────
def test_mock_worker_answers_422_when_an_identity_field_disagrees(coord):
    """The mock's gate is proved to fire by posting a deliberately wrong body
    directly — the client itself never builds one."""
    coord.state.add_pending(pending_row(502, score=-8.42))
    signer = Signer.generate("test-worker")
    coord.state.open_validator_registration(signer.wallet)
    client = CoordinatorClient(coord.url, signer, timeout=5, max_attempts=1)

    good = contract.verdict_body(
        502, -8.5, "RTX 5060", "confirm",
        molecule_hash="mol_000001f6", target="CDK4",
        miner_wallet="4bXgZ1111111111111111111111111111111111111",
        claimed_score=-8.42)
    assert client.post_verdict(good).status == 200

    for field, wrong in (("molecule_hash", "f" * 32),
                         ("target", "ESR1"),
                         ("miner_wallet", "A6oFZC1eERz5TFygucCeNF6qFvEGqB1WLMktP99rD9J"),
                         ("claimed_score", -9.999)):
        body = dict(good)
        body[field] = wrong
        from coordinator_client.client import HttpError
        with pytest.raises(HttpError) as exc:
            client.post_verdict(body)
        assert exc.value.status == 422, f"{field} should be refused with 422"
        assert field in exc.value.body


def test_a_422_is_not_recorded_as_a_verdict_and_is_not_marked_done(
        coord, tmp_path, msa_dir, protein_pins):
    """A client whose body disagreed must not retire the result locally.

    The row the client scores is the ORIGINAL one; the Worker's stored copy is
    then changed underneath it, so the (correct) body the client builds no
    longer matches what the Worker holds — exactly the 422 case.
    """
    from coordinator_client.contract import PendingResult

    row = pending_row(503, score=-8.85, **protein_pins)
    coord.state.add_pending(row)
    svc, _ = _service(coord, tmp_path, msa_dir)
    item = PendingResult.from_dict(row)

    coord.state.pending[503]["molecule_hash"] = "deadbeef" * 4

    outcome = svc._handle(item)
    assert outcome == "errors", outcome
    assert coord.state.verdicts == []
    assert not svc.store.is_done(503, "0")


def test_missing_fields_are_refused_only_from_log_v2_from_day(coord):
    """Below the flag day the four fields are optional; from it they are
    required.  Same body, two answers — that is the whole activation rule."""
    coord.state.add_pending(pending_row(504, score=-8.42))
    signer = Signer.generate("test-worker")
    coord.state.open_validator_registration(signer.wallet)
    client = CoordinatorClient(coord.url, signer, timeout=5, max_attempts=1)

    legacy = contract.verdict_body(504, -8.5, "RTX 5060", "confirm")
    assert client.post_verdict(legacy).status == 200

    coord.state.require_v2_verdict_fields = True
    from coordinator_client.client import HttpError
    with pytest.raises(HttpError) as exc:
        client.post_verdict(contract.verdict_body(504, -8.5, "RTX 5060", "confirm"))
    assert exc.value.status == 422


# ── 3. the body signed == the body sent ─────────────────────────────────────
def test_the_body_that_was_signed_is_exactly_the_body_that_was_sent(
        coord, tmp_path, msa_dir, protein_pins):
    """Independent Ed25519 verification over the EXACT received bytes.

    The mock stores the raw body it hashed for the signature check.  Here the
    canonical message is rebuilt from those bytes and verified against the
    validator's public key with solders directly — not through any client
    helper — and only then are the JSON fields compared.  A client that
    signed one body and sent another could not pass this.
    """
    row = pending_row(505, score=-8.85, target="CDK4", **protein_pins)
    coord.state.add_pending(row)
    svc, signer = _service(coord, tmp_path, msa_dir)
    assert svc.process_batch()["verdicts"] == 1

    rec = [r for r in coord.state.signed_requests if r["path"] == "/verdicts"][-1]
    assert rec["wallet"] == signer.wallet

    raw = rec["body"]
    message = (f"POST\n/verdicts\n{hashlib.sha256(raw).hexdigest()}\n"
               f"{rec['nonce']}\n{rec['timestamp']}").encode()
    pub = Pubkey.from_bytes(base58.b58decode(signer.wallet))
    sig = Signature.from_bytes(bytes.fromhex(rec["signature"]))
    assert sig.verify(pub, message), "signature does not cover the received body"

    # …and the bytes that were signed really do carry the four fields.
    body = json.loads(raw)
    assert body["molecule_hash"] == row["molecule_hash"]
    assert body["target"] == row["target"]
    assert body["miner_wallet"] == row["wallet"]
    assert body["claimed_score"] == row["score"]

    # The recorded body hash is the hash of those very bytes (no re-encoding
    # happened between signing and sending).
    assert rec["body_sha256"] == hashlib.sha256(raw).hexdigest()
    from coordinator_client.signing import canonical_body
    assert canonical_body(body) == raw


def test_signing_logic_itself_is_unchanged():
    """`Signer.auth_header` still signs METHOD\\nPATH\\nbody_sha\\nnonce\\nts.

    C47 adds fields to the BODY, never a field to the signed message, so this
    pins the message construction against accidental change.
    """
    s = Signer.generate()
    body = contract.verdict_body(
        1, -1.0, None, None, molecule_hash="aa", target="ESR1",
        miner_wallet="4K5sRsNJYvzXjUy9k5MceVgZo8CBUjnMzv44Q2sdPFGY",
        claimed_score=-1.0)
    from coordinator_client.signing import canonical_body
    raw = canonical_body(body)
    hdr = json.loads(s.auth_header("POST", "/verdicts", raw,
                                   nonce="a" * 32, timestamp=1794010013))
    expected = (f"POST\n/verdicts\n{hashlib.sha256(raw).hexdigest()}\n"
                f"{'a' * 32}\n1794010013").encode()
    pub = Pubkey.from_bytes(base58.b58decode(s.wallet))
    assert Signature.from_bytes(bytes.fromhex(hdr["signature"])).verify(pub, expected)
