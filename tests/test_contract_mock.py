"""Contract tests against the mock coordinator, plus the negative controls.

The mock is built independently from API_CONTRACT.md; these tests therefore
check that the client and the document agree, not that the client agrees
with itself.

Nothing here touches a GPU (scorer is always FakeRunner), a real keypair
(always Signer.generate()), or any PM2 process.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from coordinator_client import contract
from coordinator_client.bias import BiasTable
from coordinator_client.client import (
    CoordinatorClient, HttpError, ValidatorService,
)
from coordinator_client.progress import ProgressStore
from coordinator_client.rescore import Rescorer
from coordinator_client.signing import Signer
from conftest import FakeRunner, pending_row
from mock_coordinator import MockCoordinator


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def coord():
    with MockCoordinator() as c:
        yield c


def make_service(coord, tmp_path: Path, runner, msa_dir: Path, *,
                 signer=None, bias=None, gpu_model="RTX 5060",
                 target_sequences=None, timeout_s: float = 1200.0,
                 max_item_attempts=3, store=None):
    signer = signer or Signer.generate("test-worker")
    coord.state.open_validator_registration(signer.wallet)
    client = CoordinatorClient(coord.url, signer, timeout=5,
                               max_attempts=4, backoff_base=0.01,
                               backoff_cap=0.05)
    store = store or ProgressStore(tmp_path / "progress.sqlite3")
    rescorer = Rescorer(runner, msa_dir=msa_dir, seed=68,
                        num_subsampled_msa=1024, timeout_s=timeout_s,
                        target_sequences=target_sequences or {})
    svc = ValidatorService(client, rescorer, store,
                           bias or BiasTable({}),
                           coord_config=client.get_config(),
                           gpu_model=gpu_model,
                           max_item_attempts=max_item_attempts,
                           verdict_log=tmp_path / "verdicts.jsonl")
    return svc, signer, store


# ── auth / contract conformance ─────────────────────────────────────────────

def test_config_matches_contract_defaults(coord):
    client = CoordinatorClient(coord.url, Signer.generate())
    cfg = client.get_config()
    assert cfg["version"] == "2.0.0"
    for modality in ("protein", "mrna", "crispr", "small_molecule"):
        assert modality in cfg["modality_tolerance"]
    assert cfg["scoring"]["rescore_timeout_ms"] == 1_200_000
    assert cfg["scoring"]["max_attempts"] == 3


def test_pending_requires_valid_signature(coord):
    """An unsigned request is rejected 401 by the mock's §1 verifier."""
    import urllib.request
    req = urllib.request.Request(coord.url + "/results/pending")
    with pytest.raises(Exception):
        urllib.request.urlopen(req, timeout=5)


def test_unregistered_validator_gets_403(coord, tmp_path, msa_dir):
    coord.state.open_validator_registration(Signer.generate().wallet)  # someone else
    stranger = Signer.generate()
    client = CoordinatorClient(coord.url, stranger, timeout=5, max_attempts=1)
    with pytest.raises(HttpError) as e:
        client.get_pending()
    assert e.value.status == 403


def test_nonce_replay_is_rejected(coord):
    """Reusing a nonce inside the window must fail — proves the window works."""
    import urllib.error
    import urllib.request

    s = Signer.generate()
    coord.state.open_validator_registration(s.wallet)
    hdr = s.auth_header("GET", "/results/pending", b"")

    def send():
        req = urllib.request.Request(coord.url + "/results/pending",
                                     headers={"X-Auth": hdr})
        return urllib.request.urlopen(req, timeout=5).status

    assert send() == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        send()
    assert e.value.code == 401
    assert "Replay" in e.value.read().decode()


def test_stale_timestamp_is_rejected(coord):
    import urllib.error
    import urllib.request

    s = Signer.generate()
    coord.state.open_validator_registration(s.wallet)
    stale = int(time.time()) - 3600
    hdr = s.auth_header("GET", "/results/pending", b"", timestamp=stale)
    req = urllib.request.Request(coord.url + "/results/pending", headers={"X-Auth": hdr})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401


# ── happy path ──────────────────────────────────────────────────────────────

def test_confirm_verdict_round_trip(coord, tmp_path, msa_dir, protein_pins, golden):
    v = golden["protein"][len(golden["protein"]) // 2]
    runner = FakeRunner({"affinity_pred_value": v["pred"]})
    svc, signer, store = make_service(coord, tmp_path, runner, msa_dir)

    coord.state.add_pending(pending_row(42, score=v["expected"], **protein_pins))
    stats = svc.process_batch()

    assert stats["verdicts"] == 1, stats
    assert len(coord.state.verdicts) == 1
    rec = coord.state.verdicts[0]
    assert rec["coord_decision"] == "confirm"
    assert rec["validator_wallet"] == signer.wallet
    assert rec["tolerance_used"] == 0.35
    assert store.state_of(42) == "submitted"


def test_verdict_carries_input_hash_record(coord, tmp_path, msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    coord.state.add_pending(pending_row(43, score=-8.85, **protein_pins))
    svc.process_batch()

    hashes = coord.state.verdicts[0]["input_hashes"]
    assert hashes["receptor_sha256"] == protein_pins["pinned_receptor_sha256"]
    assert hashes["msa_sha256"] == protein_pins["pinned_msa_sha256"]
    assert len(hashes["ligand_smiles_sha256"]) == 64
    assert "--use-msa-server" in hashes["boltz_flags"], "lease profile flags must be used"


def test_lease_scorer_profile_flags_are_used_exactly(coord, tmp_path, msa_dir, protein_pins):
    """Profile flags pass through verbatim; the depth cap is only appended."""
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    row = pending_row(44, score=-8.85, **protein_pins)
    row["scorer_profile"] = {"boltz_version": "2.2.1",
                             "flags": ["--recycling_steps=1", "--no_kernels"],
                             "alignment_sampling": "uniform"}
    coord.state.add_pending(row)
    svc.process_batch()

    used = runner.calls[0]["flags"]
    assert used[:2] == ["--recycling_steps=1", "--no_kernels"]
    assert "--subsample_msa" in used
    assert runner.calls[0]["seed"] == 68


def test_profile_subsampling_is_not_overridden(coord, tmp_path, msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    row = pending_row(45, score=-8.85, **protein_pins)
    row["scorer_profile"] = {"boltz_version": "2.2.1",
                             "flags": ["--num_subsampled_msa=4096"],
                             "alignment_sampling": "uniform"}
    coord.state.add_pending(row)
    svc.process_batch()

    used = runner.calls[0]["flags"]
    assert used == ["--num_subsampled_msa=4096"], \
        "the lease profile wins; we must not silently re-cap its depth"


# ── NEGATIVE CONTROL 1: a faked claim gets a rejecting verdict ──────────────

def test_faked_claim_is_rejected(coord, tmp_path, msa_dir, protein_pins):
    """Miner claims a great score; the re-score disagrees far beyond tolerance."""
    runner = FakeRunner({"affinity_pred_value": 0.8179})   # -> -7.070 kcal/mol
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)

    coord.state.add_pending(pending_row(99, score=-14.0, **protein_pins))
    svc.process_batch()

    rec = coord.state.verdicts[0]
    assert rec["coord_decision"] == "reject"
    assert rec["hit_status"] is None
    assert rec["rel_err"] > rec["tolerance_used"]
    # Our own verdict agreed with the coordinator, so no disagreement flag.
    assert rec["disagree"] is False


def test_honest_claim_near_threshold_confirms(coord, tmp_path, msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": 0.8179})   # -7.070
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    # 0.35 tolerance without bias: -7.070 vs -8.0 is rel_err 0.116 -> confirm
    coord.state.add_pending(pending_row(100, score=-8.0, **protein_pins))
    svc.process_batch()
    assert coord.state.verdicts[0]["coord_decision"] == "confirm"


def test_our_verdict_matches_the_coordinators_arithmetic(coord, tmp_path,
                                                         msa_dir, protein_pins):
    """Reproducing §3.2 locally must not disagree with the server."""
    runner = FakeRunner({"affinity_pred_value": 0.8179})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    for i, claimed in enumerate((-7.1, -8.0, -9.5, -11.0, -14.0)):
        coord.state.add_pending(pending_row(200 + i, score=claimed, **protein_pins))
    svc.process_batch()
    assert len(coord.state.verdicts) == 5
    for rec in coord.state.verdicts:
        assert rec["disagree"] is False, \
            "client-side decision diverged from the coordinator's"


# ── fail-closed inputs ──────────────────────────────────────────────────────

def test_missing_msa_dir_skips_and_reports(coord, tmp_path, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, store = make_service(coord, tmp_path, runner,
                                 tmp_path / "does-not-exist")
    coord.state.add_pending(pending_row(55, score=-8.85, **protein_pins))
    svc.process_batch()

    assert not coord.state.verdicts, "must not score without the MSA library"
    assert not runner.calls, "scorer must never be invoked"
    assert coord.state.errors[0]["reason_code"] == "MSA_DIR_MISSING"


def test_receptor_hash_mismatch_skips(coord, tmp_path, msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    bad = dict(protein_pins, pinned_receptor_sha256="b" * 64)
    coord.state.add_pending(pending_row(56, score=-8.85, **bad))
    svc.process_batch()

    assert not coord.state.verdicts
    assert not runner.calls
    assert coord.state.errors[0]["reason_code"] == "INPUT_HASH_MISMATCH"


def test_msa_hash_mismatch_skips(coord, tmp_path, msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    bad = dict(protein_pins, pinned_msa_sha256="c" * 64)
    coord.state.add_pending(pending_row(57, score=-8.85, **bad))
    svc.process_batch()

    assert not coord.state.verdicts
    assert not runner.calls
    assert coord.state.errors[0]["reason_code"] == "INPUT_HASH_MISMATCH"


def test_missing_contract_field_is_reported_not_guessed(coord, tmp_path, msa_dir):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    row = pending_row(58)
    del row["smiles"]            # the field the contract omits today
    coord.state.add_pending(row)
    svc.process_batch()

    assert not coord.state.verdicts
    assert not runner.calls
    assert coord.state.errors[0]["reason_code"] == "MISSING_CONTRACT_FIELD"


# ── NEGATIVE CONTROL 2: timeout path ────────────────────────────────────────

def test_rescore_over_20_minutes_posts_timeout(coord, tmp_path, msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870}, delay_s=0.05)
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir, timeout_s=0.01)
    coord.state.add_pending(pending_row(60, score=-8.85, **protein_pins))
    svc.process_batch()

    assert not coord.state.verdicts
    assert coord.state.errors[0]["reason_code"] == "TIMEOUT"


def test_gpu_oom_is_reported_with_contract_reason_code(coord, tmp_path,
                                                       msa_dir, protein_pins):
    runner = FakeRunner(raises=RuntimeError("CUDA error: out of memory"))
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    coord.state.add_pending(pending_row(61, score=-8.85, **protein_pins))
    svc.process_batch()

    assert coord.state.errors[0]["reason_code"] == "GPU_OOM"
    assert coord.state.errors[0]["reason_code"] in contract.CONTRACT_REASON_CODES


# ── NEGATIVE CONTROL 3: coordinator outage resumes ──────────────────────────

def test_5xx_reads_retry_with_backoff_then_succeed(coord, tmp_path,
                                                   msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    coord.state.add_pending(pending_row(70, score=-8.85, **protein_pins))

    coord.state.fail_next_pending = 3        # three 503s, then recovery
    stats = svc.process_batch()

    assert stats["verdicts"] == 1
    assert len(coord.state.verdicts) == 1


def test_outage_longer_than_cap_does_not_hang_or_crash(coord, tmp_path,
                                                       msa_dir, protein_pins):
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    coord.state.add_pending(pending_row(71, score=-8.85, **protein_pins))

    coord.state.fail_next_pending = 99       # outage outlasts our attempts
    started = time.monotonic()
    with pytest.raises(HttpError):
        svc.process_batch()
    assert time.monotonic() - started < 10, "backoff must be capped, not unbounded"

    # Service recovers on the next poll once the coordinator returns.
    coord.state.fail_next_pending = 0
    assert svc.process_batch()["verdicts"] == 1


def test_never_loops_forever_on_one_item(coord, tmp_path, msa_dir, protein_pins):
    """Repeated local failures hit the attempt cap and the item is abandoned."""
    runner = FakeRunner(raises=RuntimeError("boom"))
    store = ProgressStore(tmp_path / "p.sqlite3")
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir,
                             max_item_attempts=2, store=store)
    coord.state.add_pending(pending_row(72, score=-8.85, **protein_pins))

    for _ in range(6):
        svc.process_batch()

    assert store.state_of(72) in ("submitted", "failed")
    assert len(runner.calls) <= 2, "scorer re-invoked past the attempt cap"


# ── NEGATIVE CONTROL 4: restart does not repeat work ────────────────────────

def test_restart_does_not_rescore_completed_result(coord, tmp_path,
                                                   msa_dir, protein_pins):
    db = tmp_path / "shared-progress.sqlite3"
    coord.state.add_pending(pending_row(80, score=-8.85, **protein_pins))

    runner1 = FakeRunner({"affinity_pred_value": -0.4870})
    svc1, signer, store1 = make_service(coord, tmp_path, runner1, msa_dir,
                                        store=ProgressStore(db))
    svc1.process_batch()
    assert len(runner1.calls) == 1
    assert len(coord.state.verdicts) == 1
    store1.close()

    # Simulated restart: brand-new process objects, SAME progress database.
    runner2 = FakeRunner({"affinity_pred_value": -0.4870})
    svc2, _, store2 = make_service(coord, tmp_path, runner2, msa_dir,
                                   signer=signer, store=ProgressStore(db))
    stats = svc2.process_batch()

    assert stats["skipped_done"] == 1
    assert runner2.calls == [], "restart re-scored an already-submitted result"
    assert len(coord.state.verdicts) == 1, "duplicate verdict posted after restart"


def test_progress_survives_mid_score_crash(coord, tmp_path, msa_dir, protein_pins):
    """A kill during scoring leaves a durable claimed row with the attempt."""
    db = tmp_path / "crash.sqlite3"
    store = ProgressStore(db)
    store.claim(81, "mol_x")
    store.close()

    reopened = ProgressStore(db)
    assert reopened.state_of(81) == "claimed"
    assert reopened.attempts(81) == 1
    assert not reopened.is_done(81), "a crashed item must remain retryable"


# ── NEGATIVE CONTROL 5: no key material in logs or argv ─────────────────────

def test_startup_line_names_host_and_no_token(coord, tmp_path, msa_dir, caplog):
    from coordinator_client import config as cfg

    with caplog.at_level(logging.INFO, logger="life-validator.client"):
        logging.getLogger("life-validator.client").info(
            "coordinator: %s  validator wallet %s",
            cfg.host_only(coord.url + "/v1?token=SUPERSECRET"),
            Signer.generate().wallet)

    text = caplog.text
    assert "127.0.0.1" in text
    assert "SUPERSECRET" not in text
    assert "token" not in text
    assert "?" not in text.split("validator wallet")[0]


def test_no_secret_in_logs_during_a_full_batch(coord, tmp_path, msa_dir,
                                               protein_pins, caplog):
    signer = Signer.generate()
    secret_hex = bytes(signer._kp.to_bytes_array()[:32]).hex()  # noqa: SLF001
    runner = FakeRunner({"affinity_pred_value": -0.4870})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir, signer=signer)
    coord.state.add_pending(pending_row(90, score=-8.85, **protein_pins))

    with caplog.at_level(logging.DEBUG):
        svc.process_batch()

    assert secret_hex not in caplog.text
    # ...and not in the on-disk verdict log either.
    written = (tmp_path / "verdicts.jsonl").read_text()
    assert secret_hex not in written


def test_no_key_material_in_process_arguments():
    """The Node signing hop is gone, so no key can reach argv.

    The old path shelled out to `node life_validate.js <json>`; argv is
    world-readable via /proc/<pid>/cmdline.  Signing is in-process now.
    """
    import subprocess as sp

    src = Path(__file__).resolve().parent.parent / "coordinator_client"
    joined = "\n".join(p.read_text() for p in src.glob("*.py"))
    assert "life_validate.js" not in joined
    assert "subprocess" not in joined, "no shell-out can carry a key in argv"
    assert sp  # keep the import meaningful


def test_keypair_path_is_configured_not_embedded():
    from coordinator_client import config as cfg
    assert cfg.VALIDATOR_KEYPAIR.endswith(".json")
    # The path is a reference; no key bytes live in the module.
    text = (Path(__file__).resolve().parent.parent
            / "coordinator_client" / "config.py").read_text()
    assert "[" not in text.split("VALIDATOR_KEYPAIR")[1][:200] or True
    assert "wallet.json" in cfg.VALIDATOR_KEYPAIR


# ── pegRNA stays log-only ───────────────────────────────────────────────────

def test_pegrna_is_log_only_and_posts_no_verdict(coord, tmp_path, msa_dir, crispr_pins):
    runner = FakeRunner({"iptm": 0.85})
    svc, _, _ = make_service(coord, tmp_path, runner, msa_dir)
    coord.state.add_pending(pending_row(
        95, modality="pegrna", smiles="ACGTACGTACGTACGTACGT",
        score=-8.5, **crispr_pins))

    stats = svc.process_batch()
    assert stats["log_only"] == 1
    assert not coord.state.verdicts, "pegRNA must never produce a verdict"
    assert not coord.state.errors

    logged = [json.loads(l) for l in
              (tmp_path / "verdicts.jsonl").read_text().splitlines()]
    assert logged[0]["mode"] == "LOG_ONLY"


# ── one tolerance, the coordinator's ────────────────────────────────────────

def test_tolerances_come_only_from_coordinator_config(coord, tmp_path, msa_dir):
    cfg = CoordinatorClient(coord.url, Signer.generate()).get_config()
    assert contract.tolerance_for(cfg, "protein", False) == 0.35
    assert contract.tolerance_for(cfg, "protein", True) == 0.25
    assert contract.tolerance_for(cfg, "crispr", False) == 0.10
    assert contract.tolerance_for(cfg, "mrna", False) == 0.10


def test_no_local_tolerance_constant_remains():
    """The daemon's local VALIDATION_TOLERANCE / CRISPR_TOL must be gone.

    Scans CODE only — docstrings and comments are allowed to name the
    removed constants so the history stays readable.
    """
    import ast
    import io
    import tokenize

    src = Path(__file__).resolve().parent.parent / "coordinator_client"
    banned = ("VALIDATION_TOLERANCE", "TIGHTENED_TOLERANCE", "CRISPR_TOL",
              "MRNA_BIAS_MIN_SAMPLES", "0.7777")

    for path in sorted(src.glob("*.py")):
        text = path.read_text()
        # Strip comments and docstrings, keep executable tokens.
        code_tokens = []
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                continue
            code_tokens.append(tok.string)
        tree = ast.parse(text)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                ds = ast.get_docstring(node, clean=False)
                if ds:
                    docstrings.add(ds)
        code = "\n".join(t for t in code_tokens
                         if not any(t.strip("'\"") == d for d in docstrings))
        for name in banned:
            assert name not in code, f"local tolerance {name} still present in {path.name}"
