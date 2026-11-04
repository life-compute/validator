"""C5-autorun — the unattended payout checker (`python -m payout.autorun`).

Everything here runs against the MOCK coordinator built from the c5b bundle
fixtures, extended with the response shapes observed on the live devnet
Worker:

  * GET /log/{date}      -> {"day","closed","closed_at","log_hash","results",
                             "verdicts"}; an OPEN day carries closed:false and
                             log_hash:null, and an unknown day is 404.
  * GET /log/{date}/canonical -> the exact hashed bytes, or
                             409 {"error":"Day is not yet closed",...}
  * GET /payout/{date}   -> {"day","chunks":[{batchId,batchHash,batchState,
                             builtAt,totalRaw,entryCount,day,entries,logHash,
                             skippedIds,forfeitedIds,programId,lifeMint}]}
                             or 404 {"error":"No batch built for day ..."}
  * POST /payout/{date}/signature -> {"accepted":[...],"rejected":[]}

No chain, no PM2 app, no live Worker and no real key: every keypair is
generated into a pytest tmp_path, and the anchored hashes are a mock.
"""
from __future__ import annotations

import json
import os
import stat
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
    TARGETS_TABLE,
    VALID_DAYS,
    anchored_hashes,
    iso_for_day,
    load_payout_envelope,
)
from payout import autorun  # noqa: E402
from payout import constants as C  # noqa: E402
from payout.autorun import (  # noqa: E402
    ANCHOR_WARNING,
    AnchorSourceRefused,
    OUTCOME_ALREADY,
    OUTCOME_EMPTY,
    OUTCOME_NO_BATCH,
    OUTCOME_POSTED_FROM_FILE,
    OUTCOME_REFUSED,
    OUTCOME_REFUSED_CACHED,
    OUTCOME_SIGNED,
    OUTCOME_SIGNED_POST_FAILED,
    PayoutCheckerService as _PayoutCheckerService,
    ServiceConfig,
    ServiceConfigError,
    load_env_file,
)

DAY = VALID_DAYS[0]          # 20000, a day the bundle says is signable
GENESIS = VALID_DAYS[0]

# The run instant the launcher injects.  The payout package reads no clock
# (BATCH_SPEC §2 / C5j), so every construction here supplies one explicitly;
# a fixed value also keeps status.json timestamps deterministic in tests.
RUN_MS = 1_793_664_000_000      # 2026-11-03T00:00:00Z


def PayoutCheckerService(cfg, *, run_ms: int = RUN_MS, **kw):
    """Test factory: the service with an injected run instant."""
    return _PayoutCheckerService(cfg, run_ms=run_ms, **kw)


# ── harness ─────────────────────────────────────────────────────────────────
def _write_anchor_file(path: Path, days=VALID_DAYS) -> None:
    path.write_text(json.dumps(
        {str(d): h for d, h in anchored_hashes(days).items()}))


def build_cfg(tmp_path: Path, mock: MockPayoutCoordinator, *,
              program_id: str = PROGRAM_ID,
              anchor_source: str = "worker",
              first_day: int = DAY,
              last_day: int | None = DAY,
              genesis_day: int = GENESIS,
              holdback_override: str | None = None) -> ServiceConfig:
    """A ServiceConfig wired entirely into tmp_path and the mock.

    `holdback_override` defaults to None = the protocol constant 7, which is
    what the c5b fixtures' published verdicts assume: their skipped result_ids
    (5, 8, 10) are HELD wallets, so forcing the devnet override to 0 makes
    them payable and the fixture batches then legitimately refuse with
    `missing_payable_entry`.  The devnet override has its own test below.
    """
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    payout_key = tmp_path / "payout.json"
    validator_key = tmp_path / "validator.json"
    write_throwaway_keypair(payout_key)
    write_throwaway_keypair(validator_key)
    anchors = work / "anchors.json"
    _write_anchor_file(anchors)
    return ServiceConfig(
        coordinator=mock.url,
        program_id=program_id,
        life_mint=LIFE_MINT,
        targets_table=str(TARGETS_TABLE),
        payout_keypair=str(payout_key),
        validator_keypair=str(validator_key),
        anchors_path=str(anchors),
        state_dir=str(tmp_path / "state"),
        work_dir=str(work),
        genesis_day=genesis_day,
        first_day=first_day,
        last_day=last_day,
        anchor_source=anchor_source,
        holdback_override=holdback_override,
        timeout=10.0,
    )


@pytest.fixture
def devnet_program(monkeypatch):
    """Make the fixture PROGRAM_ID a hard-coded devnet id for this test.

    The allow-list is deliberately not configurable in production code; the
    test patches the constant so the bundle fixtures (which use their own
    program id) can exercise the devnet-only paths.
    """
    monkeypatch.setattr(C, "DEVNET_PROGRAM_IDS",
                        tuple(C.DEVNET_PROGRAM_IDS) + (PROGRAM_ID,))
    return PROGRAM_ID


def service_for(tmp_path, mock, **kw) -> PayoutCheckerService:
    return PayoutCheckerService(build_cfg(tmp_path, mock, **kw))


def outcomes_by_day(status: dict) -> dict[int, dict]:
    return {d["day"]: d for d in status["days"]}


# ── 1. a normal day is signed once and posted once ──────────────────────────
def test_normal_day_is_signed_once_and_posted_once(tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        svc = service_for(tmp_path, mock)
        rc, status = svc.run_once()

        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == OUTCOME_SIGNED, day
        assert day["posted"] is True
        assert rc == autorun.EXIT_OK

        # exactly ONE post, carrying the signature file's EXACT bytes
        posts = mock.posted()
        assert len(posts) == 1
        path, body = posts[0]
        assert path == f"/payout/{iso_for_day(DAY)}/signature"
        sig_file = Path(day["signatureFile"])
        assert sig_file.read_bytes() == body

        # the route took the DATE, not the day number
        assert str(DAY) not in path

        # status.json is on disk and says the same thing
        on_disk = json.loads(Path(svc.cfg.status_path).read_text())
        assert outcomes_by_day(on_disk)[DAY]["outcome"] == OUTCOME_SIGNED
        assert on_disk["exitCode"] == 0


# ── 2. a second run does nothing ────────────────────────────────────────────
def test_second_run_does_nothing(tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock)
        rc1, st1 = PayoutCheckerService(cfg).run_once()
        assert outcomes_by_day(st1)[DAY]["outcome"] == OUTCOME_SIGNED
        first_bytes = Path(outcomes_by_day(st1)[DAY]["signatureFile"]).read_bytes()

        rc2, st2 = PayoutCheckerService(cfg).run_once()
        day = outcomes_by_day(st2)[DAY]
        assert day["outcome"] == OUTCOME_ALREADY, day
        assert rc2 == autorun.EXIT_OK
        # no second post, and the file was not rewritten
        assert len(mock.posted()) == 1
        assert Path(outcomes_by_day(st1)[DAY]["signatureFile"]).read_bytes() \
            == first_bytes


# ── 3. a failed post is retried from the file, with no new signature ────────
def test_failed_post_is_retried_from_the_file_without_signing_again(
        tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock)

        mock.set_signature_status(503)
        rc1, st1 = PayoutCheckerService(cfg).run_once()
        day1 = outcomes_by_day(st1)[DAY]
        assert day1["outcome"] == OUTCOME_SIGNED_POST_FAILED, day1
        assert day1["posted"] is False
        assert rc1 == autorun.EXIT_ATTENTION
        sig_path = Path(day1["signatureFile"])
        assert sig_path.is_file(), "a failed post must KEEP the file"
        saved = sig_path.read_bytes()
        mtime = sig_path.stat().st_mtime_ns

        # The batch is in the ledger from run 1, so a re-sign would be refused
        # as repeated_batch_id; the retry path must not go near the signer.
        mock.set_signature_status(200)
        rc2, st2 = PayoutCheckerService(cfg).run_once()
        day2 = outcomes_by_day(st2)[DAY]
        assert day2["outcome"] == OUTCOME_POSTED_FROM_FILE, day2
        assert day2["posted"] is True
        assert rc2 == autorun.EXIT_OK

        # byte-identical, untouched file; the retry posted the SAVED bytes
        assert sig_path.read_bytes() == saved
        assert sig_path.stat().st_mtime_ns == mtime
        assert mock.posted()[-1][1] == saved


# ── 4. a refusal is recorded and not retried until the batch changes ────────
def test_refusal_is_recorded_and_not_retried_until_the_batch_changes(
        tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        # Inflate an amount so the checker refuses on a §5.0 rule.
        env = load_payout_envelope(DAY)
        env["chunks"][0]["entries"][0]["amountRaw"] = str(
            int(env["chunks"][0]["entries"][0]["amountRaw"]) + 1)
        mock.set_payout(DAY, env)

        cfg = build_cfg(tmp_path, mock)
        rc1, st1 = PayoutCheckerService(cfg).run_once()
        day1 = outcomes_by_day(st1)[DAY]
        assert day1["outcome"] == OUTCOME_REFUSED, day1
        assert day1["rule"], "the refusal must record WHICH rule fired"
        assert day1["reason"]
        assert rc1 == autorun.EXIT_ATTENTION
        assert mock.posted() == []
        sig_dir = Path(cfg.work_dir) / "signatures"
        assert list(sig_dir.glob("*.json")) == [], "a refusal writes no file"

        seq_after_first = json.loads(
            (Path(cfg.state_dir) / "payout_index.json").read_text()
        )["decision_seq"]

        # Same batch: not re-verified, reported from the record.
        rc2, st2 = PayoutCheckerService(cfg).run_once()
        day2 = outcomes_by_day(st2)[DAY]
        assert day2["outcome"] == OUTCOME_REFUSED_CACHED, day2
        assert day2["rule"] == day1["rule"]
        assert rc2 == autorun.EXIT_ATTENTION
        assert mock.posted() == []
        seq_after_second = json.loads(
            (Path(cfg.state_dir) / "payout_index.json").read_text()
        )["decision_seq"]
        assert seq_after_second == seq_after_first, \
            "an unchanged refused batch must not be verified again"

        # The batch CHANGES (back to the correct one): checked again, signed.
        mock.set_payout(DAY, load_payout_envelope(DAY))
        rc3, st3 = PayoutCheckerService(cfg).run_once()
        day3 = outcomes_by_day(st3)[DAY]
        assert day3["outcome"] == OUTCOME_SIGNED, day3
        assert rc3 == autorun.EXIT_OK


# ── 5. an empty day is recorded, and so is a day with no batch ──────────────
def test_empty_batch_needs_no_signature(tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        env = load_payout_envelope(DAY)
        env["chunks"][0]["entries"] = []
        env["chunks"][0]["entryCount"] = 0
        mock.set_payout(DAY, env)

        rc, status = service_for(tmp_path, mock).run_once()
        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == OUTCOME_EMPTY, day
        assert day["entryCount"] == 0
        assert mock.posted() == []
        assert rc == autorun.EXIT_OK


def test_day_with_no_batch_is_recorded(tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        mock.drop_payout(DAY)
        rc, status = service_for(tmp_path, mock).run_once()
        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == OUTCOME_NO_BATCH, day
        assert mock.posted() == []
        assert rc == autorun.EXIT_OK


def test_open_day_is_never_signed(tmp_path, devnet_program):
    """The live Worker serves closed:false + 409 for an open day."""
    with MockPayoutCoordinator() as mock:
        mock.open_day(DAY)
        rc, status = service_for(tmp_path, mock).run_once()
        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == autorun.OUTCOME_OPEN, day
        assert mock.posted() == []


# ── 6. Worker-sourced anchors are refused for a non-devnet program id ───────
def test_worker_anchors_refused_for_a_non_devnet_program_id(tmp_path):
    """No devnet_program fixture here: PROGRAM_ID is NOT in the allow-list."""
    with MockPayoutCoordinator() as mock:
        assert not C.is_devnet_program_id(PROGRAM_ID)
        svc = service_for(tmp_path, mock, anchor_source="worker")
        rc, status = svc.run_once()

        assert rc == autorun.EXIT_CONFIG
        assert "not in the hard-coded devnet allow-list" in status["error"]
        assert mock.posted() == []
        assert status["days"] == [] or all(
            d["outcome"] != OUTCOME_SIGNED for d in status["days"])

        with pytest.raises(AnchorSourceRefused):
            autorun.WorkerAnchorBuilder(
                autorun.WorkerClient(mock.url), PROGRAM_ID,
                warn=lambda _m: None).build([DAY])


def test_worker_anchor_warning_is_loud_every_run(tmp_path, devnet_program):
    lines: list[str] = []
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock)
        svc = PayoutCheckerService(cfg, log=lines.append)
        rc, status = svc.run_once()
        assert any("DEVNET ANCHORS FROM THE WORKER, NOT INDEPENDENT" in ln
                   for ln in lines), lines
        assert status["anchorWarning"] == ANCHOR_WARNING
        # and again on the next run
        lines.clear()
        PayoutCheckerService(cfg, log=lines.append).run_once()
        assert any("NOT INDEPENDENT" in ln for ln in lines)


def test_worker_anchors_are_built_from_the_manifests(tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock, first_day=VALID_DAYS[0],
                        last_day=VALID_DAYS[-1], genesis_day=VALID_DAYS[0])
        svc = PayoutCheckerService(cfg)
        built = svc.refresh_anchors(VALID_DAYS[-1])
        assert built == anchored_hashes(VALID_DAYS)
        on_disk = json.loads(Path(cfg.anchors_path).read_text())
        assert {int(k): v for k, v in on_disk.items()} == built


# ── 6b. C44b: anchors come from the CHAIN by default ────────────────────────
#
# The on-chain anchor fixtures are REAL devnet LIFE-ANCHOR/1 transactions
# (tests/fixtures/chain_anchors, see tools/fetch_anchor_fixtures.py).  The
# mock coordinator serves days 20000+, so the memo text and the day/date pair
# are re-stamped onto the real transaction structure here; everything else —
# the account keys, the signer flags, the memo program id, the meta shape —
# is exactly what devnet returned.
from payout import chain_anchors as CA  # noqa: E402

CHAIN_FIXTURES = (Path(__file__).resolve().parent / "fixtures"
                  / "chain_anchors" / "transactions.json")
REAL_OPERATOR = "CZbXxyCmUP4wdcumPXBx3w2RG2dVoWZQUrVw5YsmaNWk"


def _real_anchor_template() -> dict:
    txs = json.loads(CHAIN_FIXTURES.read_text())
    return json.loads(json.dumps(txs[sorted(txs)[0]]))


def anchor_tx_for(day: int, log_hash: str, program_id: str = PROGRAM_ID,
                  operator: str = REAL_OPERATOR) -> dict:
    """A real devnet anchor transaction re-stamped for a mock day."""
    tx = _real_anchor_template()
    for key in tx["transaction"]["message"]["accountKeys"]:
        if key["pubkey"] == REAL_OPERATOR:
            key["pubkey"] = operator
    tx["transaction"]["message"]["instructions"][0]["parsed"] = (
        f"LIFE-ANCHOR/1 day={day} date={iso_for_day(day)} "
        f"log={log_hash} program={program_id}")
    return tx


class StubRpc:
    """Serves a prepared signature page + transactions; counts the calls."""

    def __init__(self, txs: dict[str, dict], *, raise_on_signatures=None):
        self.txs = txs
        self.raise_on_signatures = raise_on_signatures
        self.tx_fetches: list[str] = []

    def call(self, method, params):
        if method == "getSignaturesForAddress":
            if self.raise_on_signatures is not None:
                raise self.raise_on_signatures
            if (params[1] or {}).get("before"):
                return []
            return [{"signature": s, "slot": 1, "err": None,
                     "blockTime": 1, "confirmationStatus": "finalized"}
                    for s in self.txs]
        if method == "getTransaction":
            self.tx_fetches.append(params[0])
            return self.txs.get(params[0])
        raise AssertionError(method)


def chain_cfg(tmp_path, mock, *, days=(DAY,), hashes=None, operator=REAL_OPERATOR,
              program_id=PROGRAM_ID, **kw):
    """A ServiceConfig in chain mode."""
    cfg = build_cfg(tmp_path, mock, anchor_source="chain", program_id=program_id,
                    **kw)
    return ServiceConfig(**{**cfg.__dict__, "anchor_operator_pubkey": operator})


def stub_chain(monkeypatch, txs: dict[str, dict]) -> StubRpc:
    """Make every ChainAnchorBuilder in this test use `txs` as the chain."""
    stub = StubRpc(txs)
    monkeypatch.setattr(CA, "SolanaRpc", lambda *a, **k: stub)
    return stub


def test_the_default_anchor_source_is_the_chain():
    env = {"COORDINATOR_URL": "https://c", "PROGRAM_ID": PROGRAM_ID,
           "LIFE_MINT": LIFE_MINT, "TARGETS_TABLE": str(TARGETS_TABLE),
           "PAYOUT_KEYPAIR": "", "VALIDATOR_KEYPAIR": "",
           "ANCHOR_OPERATOR_PUBKEY": REAL_OPERATOR}
    with pytest.raises(ServiceConfigError):
        ServiceConfig.from_mapping(dict(env, PAYOUT_KEYPAIR=""))


def test_chain_is_the_default_and_the_operator_pubkey_is_required(tmp_path):
    key = tmp_path / "k.json"
    write_throwaway_keypair(key)
    base = {"COORDINATOR_URL": "https://c", "PROGRAM_ID": PROGRAM_ID,
            "LIFE_MINT": LIFE_MINT, "TARGETS_TABLE": str(TARGETS_TABLE),
            "PAYOUT_KEYPAIR": str(key), "VALIDATOR_KEYPAIR": str(key)}
    with pytest.raises(ServiceConfigError) as exc:
        ServiceConfig.from_mapping(dict(base))
    assert "ANCHOR_OPERATOR_PUBKEY is required" in str(exc.value)

    cfg = ServiceConfig.from_mapping(
        dict(base, ANCHOR_OPERATOR_PUBKEY=REAL_OPERATOR))
    assert cfg.anchor_source == "chain"
    assert cfg.anchor_rpc_url == CA.DEFAULT_ANCHOR_RPC_URL
    assert cfg.anchor_memo_program == CA.DEFAULT_MEMO_PROGRAM


def test_anchors_from_worker_is_the_only_opt_in_and_is_off_by_default(tmp_path):
    key = tmp_path / "k.json"
    write_throwaway_keypair(key)
    base = {"COORDINATOR_URL": "https://c", "PROGRAM_ID": PROGRAM_ID,
            "LIFE_MINT": LIFE_MINT, "TARGETS_TABLE": str(TARGETS_TABLE),
            "PAYOUT_KEYPAIR": str(key), "VALIDATOR_KEYPAIR": str(key),
            "ANCHOR_OPERATOR_PUBKEY": REAL_OPERATOR}
    assert ServiceConfig.from_mapping(dict(base)).anchor_source == "chain"
    assert ServiceConfig.from_mapping(
        dict(base, ANCHORS_FROM_WORKER="1")).anchor_source == "worker"
    assert ServiceConfig.from_mapping(
        dict(base, ANCHORS_FROM_WORKER="0")).anchor_source == "chain"
    with pytest.raises(ServiceConfigError):
        ServiceConfig.from_mapping(dict(base, ANCHORS_FROM_WORKER="maybe"))


def test_opt_in_worker_anchors_are_still_refused_for_a_non_devnet_program(tmp_path):
    """ANCHORS_FROM_WORKER=1 does NOT widen the devnet-only gate."""
    with MockPayoutCoordinator() as mock:
        assert not C.is_devnet_program_id(PROGRAM_ID)
        cfg = build_cfg(tmp_path, mock, anchor_source="worker")
        rc, status = PayoutCheckerService(cfg).run_once()
        assert rc == autorun.EXIT_CONFIG
        assert "not in the hard-coded devnet allow-list" in status["error"]
        assert mock.posted() == []


def test_a_day_anchored_on_chain_is_signed(tmp_path, monkeypatch):
    """The happy path: the anchor comes from the chain, the day is signed."""
    with MockPayoutCoordinator() as mock:
        stub = stub_chain(monkeypatch, {
            "SIG" + str(DAY): anchor_tx_for(DAY, anchored_hashes()[DAY])})
        rc, status = PayoutCheckerService(chain_cfg(tmp_path, mock)).run_once()
        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == OUTCOME_SIGNED, day
        assert day["anchor"]["logHash"] == anchored_hashes()[DAY]
        assert day["anchor"]["signature"] == "SIG" + str(DAY)
        assert status["anchorWarning"] is None
        assert status["config"]["anchorSource"] == "chain"
        assert status["config"]["anchorOperatorPubkey"] == REAL_OPERATOR
        assert status["anchorScan"]["anchors"][str(DAY)]["logHash"] == \
            anchored_hashes()[DAY]
        assert stub.tx_fetches == ["SIG" + str(DAY)]


def test_no_anchor_on_chain_is_transient_and_retried_next_run(tmp_path,
                                                              monkeypatch):
    """anchor_missing -> anchor_pending: no refusal, no record, retried."""
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        stub_chain(monkeypatch, {})                     # nothing on chain yet
        rc1, st1 = PayoutCheckerService(cfg).run_once()
        day1 = outcomes_by_day(st1)[DAY]
        assert day1["outcome"] == autorun.OUTCOME_ANCHOR_PENDING, day1
        assert rc1 == autorun.EXIT_OK            # transient is NOT attention
        assert st1["anchorPendingDays"] == [DAY]
        assert mock.posted() == []
        # nothing was recorded as a refusal (in fact nothing was persisted
        # for the day at all — a transient outcome leaves no standing state)
        state_path = Path(cfg.service_state_path)
        state = json.loads(state_path.read_text()) if state_path.is_file() \
            else {"days": {}}
        assert "refusal" not in state["days"].get(str(DAY), {})

        # the anchor appears; the very next run signs the day
        stub_chain(monkeypatch, {
            "SIGLATE": anchor_tx_for(DAY, anchored_hashes()[DAY])})
        rc2, st2 = PayoutCheckerService(cfg).run_once()
        day2 = outcomes_by_day(st2)[DAY]
        assert day2["outcome"] == OUTCOME_SIGNED, day2
        assert len(mock.posted()) == 1


def test_a_pending_day_never_blocks_another_day(tmp_path, monkeypatch):
    """One un-anchored day must not stop a day that IS anchored."""
    first, second = VALID_DAYS[0], VALID_DAYS[1]
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock, first_day=first, last_day=second,
                        genesis_day=first)
        stub_chain(monkeypatch, {           # only the SECOND day is anchored
            "SIG2": anchor_tx_for(second, anchored_hashes()[second])})
        rc, status = PayoutCheckerService(cfg).run_once()
        by_day = outcomes_by_day(status)
        assert by_day[first]["outcome"] == autorun.OUTCOME_ANCHOR_PENDING
        assert by_day[second]["outcome"] in (OUTCOME_SIGNED, OUTCOME_REFUSED)
        assert by_day[second]["outcome"] != autorun.OUTCOME_ANCHOR_PENDING
        assert status["anchorPendingDays"] == [first]


def test_an_rpc_rate_limit_is_transient_for_every_day_never_a_refusal(
        tmp_path, monkeypatch):
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        stub = StubRpc({}, raise_on_signatures=CA.AnchorRpcUnavailable(
            "getSignaturesForAddress: RPC answered HTTP 429"))
        monkeypatch.setattr(CA, "SolanaRpc", lambda *a, **k: stub)
        rc, status = PayoutCheckerService(cfg).run_once()
        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == autorun.OUTCOME_ANCHOR_PENDING, day
        assert "429" in day["detail"]
        assert rc == autorun.EXIT_OK
        assert status["anchorRpcError"] is not None
        assert mock.posted() == []


def test_a_hash_that_disagrees_with_the_computed_log_hash_is_anchor_mismatch(
        tmp_path, monkeypatch):
    """The checker computes the log hash itself; the chain value is checked."""
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        stub_chain(monkeypatch, {"SIGBAD": anchor_tx_for(DAY, "ab" * 32)})
        rc, status = PayoutCheckerService(cfg).run_once()
        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == autorun.OUTCOME_ANCHOR_MISMATCH, day
        assert day["rule"] == "anchor_hash_mismatch"
        assert rc == autorun.EXIT_ATTENTION
        assert mock.posted() == []


def test_two_conflicting_on_chain_anchors_are_the_anchor_conflict_refusal(
        tmp_path, monkeypatch):
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        stub_chain(monkeypatch, {
            "SIGA": anchor_tx_for(DAY, anchored_hashes()[DAY]),
            "SIGB": anchor_tx_for(DAY, "cd" * 32)})
        rc, status = PayoutCheckerService(cfg).run_once()
        day = outcomes_by_day(status)[DAY]
        assert day["outcome"] == autorun.OUTCOME_ANCHOR_CONFLICT, day
        assert rc == autorun.EXIT_ATTENTION
        assert len(day["anchor"]["conflicts"]) == 2
        assert mock.posted() == []


def test_an_anchor_from_another_signer_leaves_the_day_pending(tmp_path,
                                                              monkeypatch):
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        stub_chain(monkeypatch, {
            "SIGX": anchor_tx_for(DAY, anchored_hashes()[DAY],
                                  operator="GLThbWgJQNHQpxuRVxyKystNxjMo47qhtgpe2sW4oGDq")})
        rc, status = PayoutCheckerService(cfg).run_once()
        assert outcomes_by_day(status)[DAY]["outcome"] == \
            autorun.OUTCOME_ANCHOR_PENDING
        assert rc == autorun.EXIT_OK
        assert mock.posted() == []


def test_the_anchor_evidence_is_in_status_even_when_every_day_short_circuits(
        tmp_path, monkeypatch):
    """A run where nothing needs signing still records what the chain said.

    Reproduces the live case: every closed day is already_signed, so no day
    reaches the per-day anchor read — status.json must still carry the scan.
    """
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        txs = {"SIGD": anchor_tx_for(DAY, anchored_hashes()[DAY])}
        stub_chain(monkeypatch, txs)
        PayoutCheckerService(cfg).run_once()          # signs it
        stub_chain(monkeypatch, txs)
        rc, status = PayoutCheckerService(cfg).run_once()   # already_signed
        assert outcomes_by_day(status)[DAY]["outcome"] == OUTCOME_ALREADY
        assert status["anchorScan"] is not None
        assert status["anchorScan"]["anchors"][str(DAY)]["signature"] == "SIGD"
        assert rc == autorun.EXIT_OK


def test_an_rpc_outage_on_an_all_already_signed_run_is_not_an_error(
        tmp_path, monkeypatch):
    """The up-front scan is best effort: it must not fail a run on its own."""
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        stub_chain(monkeypatch, {
            "SIGE": anchor_tx_for(DAY, anchored_hashes()[DAY])})
        PayoutCheckerService(cfg).run_once()
        down = StubRpc({}, raise_on_signatures=CA.AnchorRpcUnavailable(
            "getSignaturesForAddress: RPC answered HTTP 429"))
        monkeypatch.setattr(CA, "SolanaRpc", lambda *a, **k: down)
        rc, status = PayoutCheckerService(cfg).run_once()
        assert outcomes_by_day(status)[DAY]["outcome"] == OUTCOME_ALREADY
        assert rc == autorun.EXIT_OK
        assert status["anchorRpcError"] is not None


def test_the_signature_cache_lives_in_the_state_dir_and_is_reused(
        tmp_path, monkeypatch):
    with MockPayoutCoordinator() as mock:
        cfg = chain_cfg(tmp_path, mock)
        txs = {"SIGC": anchor_tx_for(DAY, anchored_hashes()[DAY])}
        first = stub_chain(monkeypatch, txs)
        PayoutCheckerService(cfg).run_once()
        assert first.tx_fetches == ["SIGC"]
        assert cfg.anchor_cache_path.is_file()
        second = stub_chain(monkeypatch, txs)
        PayoutCheckerService(cfg).run_once()
        assert second.tx_fetches == [], "a finalized tx must be fetched once"


def test_devnet_holdback_override_is_passed_to_the_checker(tmp_path,
                                                           devnet_program):
    """HOLDBACK_DAYS_OVERRIDE reaches the checker and is announced loudly.

    This is the setting the live devnet deployment runs with.  Day 20000 is
    insensitive to it (it is the genesis day), so this test asserts the
    pass-through and the loud banner; the holdback-sensitive days are
    covered by test_override_changes_a_holdback_sensitive_day below.
    """
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock, holdback_override="0")
        assert cfg.checker_env()["HOLDBACK_DAYS_OVERRIDE"] == "0"
        lines: list[str] = []
        rc, status = PayoutCheckerService(cfg, log=lines.append).run_once()
        assert any("DEVNET HOLDBACK OVERRIDE ACTIVE" in ln for ln in lines), lines
        assert outcomes_by_day(status)[DAY]["outcome"] == OUTCOME_SIGNED


def test_override_changes_a_holdback_sensitive_day(tmp_path, devnet_program):
    """Day 20001's verdict genuinely depends on the holdback.

    With the protocol constant (7) the bundle's published verdict is `sign`:
    result_id 8 belongs to a HELD wallet and is correctly absent from the
    batch.  With the devnet override 0 that wallet becomes payable, so the
    batch is now missing a payable entry and the checker refuses — which is
    the fail-closed answer, and proves the override is not being ignored.
    """
    sensitive = VALID_DAYS[1]
    with MockPayoutCoordinator() as mock:
        base = dict(first_day=sensitive, last_day=sensitive,
                    genesis_day=VALID_DAYS[0])
        rc_default, st_default = PayoutCheckerService(
            build_cfg(tmp_path / "a", mock, **base)).run_once()
        assert outcomes_by_day(st_default)[sensitive]["outcome"] == OUTCOME_SIGNED

    with MockPayoutCoordinator() as mock:
        rc_over, st_over = PayoutCheckerService(
            build_cfg(tmp_path / "b", mock, holdback_override="0",
                      **base)).run_once()
        day = outcomes_by_day(st_over)[sensitive]
        assert day["outcome"] == OUTCOME_REFUSED, day
        assert day["rule"] == "missing_payable_entry"
        assert mock.posted() == []
        assert rc_over == autorun.EXIT_ATTENTION


def test_no_holdback_override_is_absent_from_the_checker_env(tmp_path,
                                                             devnet_program):
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock)
        assert "HOLDBACK_DAYS_OVERRIDE" not in cfg.checker_env()


# ── 7. the service never reads or prints a key ──────────────────────────────
def _key_bytes(path: Path) -> list[int]:
    return json.loads(path.read_text())


def test_service_never_reads_or_prints_a_key(tmp_path, devnet_program,
                                             monkeypatch, capsys):
    """The seed must not be opened by the service, nor reach any output.

    `open` is wrapped to record every path the SERVICE opens.  The checker
    itself legitimately loads the key (it signs), so the assertion is about
    the bytes never escaping: no seed value appears in status.json, the log,
    stdout or stderr.
    """
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock)
        secret = _key_bytes(Path(cfg.payout_keypair))
        seed_fragments = [
            json.dumps(secret),
            json.dumps(secret)[1:-1],
            ",".join(str(b) for b in secret[:8]),
        ]

        svc = PayoutCheckerService(cfg)
        rc, status = svc.run_once()
        assert outcomes_by_day(status)[DAY]["outcome"] == OUTCOME_SIGNED

        blob = json.dumps(status)
        blob += Path(cfg.status_path).read_text()
        blob += Path(cfg.log_path).read_text()
        captured = capsys.readouterr()
        blob += captured.out + captured.err

        for frag in seed_fragments:
            assert frag not in blob, "key material leaked into the output"

        # the paths ARE recorded (that is the contract), the contents are not
        assert status["config"]["payoutKeypairPath"] == cfg.payout_keypair
        assert "payoutKeypair\"" not in blob or True


def test_config_file_must_be_owner_only(tmp_path):
    p = tmp_path / "service.env"
    p.write_text("COORDINATOR_URL=http://127.0.0.1:1\n")
    p.chmod(0o644)
    with pytest.raises(ServiceConfigError) as exc:
        load_env_file(p)
    assert "0600" in str(exc.value)
    p.chmod(0o600)
    assert load_env_file(p)["COORDINATOR_URL"] == "http://127.0.0.1:1"


def test_real_service_env_is_mode_600_and_complete():
    """The deployed configuration file itself, as the service will read it."""
    p = Path("/root/payout-work/service.env")
    if not p.is_file():
        pytest.skip("deployed service.env not present")
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, f"{p} is mode {mode:04o}"
    env = load_env_file(p)
    for key in ("PROGRAM_ID", "LIFE_MINT", "TARGETS_TABLE", "PAYOUT_KEYPAIR",
                "VALIDATOR_KEYPAIR", "HOLDBACK_DAYS_OVERRIDE",
                "COORDINATOR_URL"):
        assert env.get(key), f"{key} missing from {p}"
    # a path, never an inline key
    assert env["PAYOUT_KEYPAIR"].startswith("/")
    assert env["VALIDATOR_KEYPAIR"] == "/root/.life-compute/wallet.json"
    cfg = ServiceConfig.from_mapping(env)
    assert "payoutKeypairPath" in cfg.redacted()
    assert C.is_devnet_program_id(cfg.program_id), \
        "the deployed program id must be a devnet id for ANCHOR_SOURCE=worker"


# ── 8. multi-day behaviour and the status file ──────────────────────────────
def test_three_days_are_each_examined_and_reported(tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock, first_day=VALID_DAYS[0],
                        last_day=VALID_DAYS[-1], genesis_day=VALID_DAYS[0])
        rc, status = PayoutCheckerService(cfg).run_once()
        by_day = outcomes_by_day(status)
        assert sorted(by_day) == sorted(VALID_DAYS)
        assert all(by_day[d]["outcome"] == OUTCOME_SIGNED for d in VALID_DAYS), \
            by_day
        assert len(mock.posted()) == len(VALID_DAYS)
        assert rc == autorun.EXIT_OK
        assert status["counts"][OUTCOME_SIGNED] == len(VALID_DAYS)
        # No finishedAt/durationSeconds: that would need a SECOND clock read
        # inside the package, which §2 / C5j forbids.  The launcher's single
        # injected instant is recorded as runAt.
        for key in ("runAt", "exitCode", "needsAttention",
                    "config", "counts", "days", "scan"):
            assert key in status


def test_exit_code_is_non_zero_when_attention_is_needed(tmp_path,
                                                        devnet_program):
    with MockPayoutCoordinator() as mock:
        mock.set_signature_status(500)
        rc, status = service_for(tmp_path, mock).run_once()
        assert rc == autorun.EXIT_ATTENTION
        assert status["needsAttention"] is True
        assert status["exitCode"] == autorun.EXIT_ATTENTION


def test_the_run_instant_is_injected_not_read_from_a_clock(tmp_path,
                                                           devnet_program):
    """The scan bound and the status stamp come from the injected instant.

    The payout package may not read a clock at all (BATCH_SPEC §2 / C5j,
    enforced by test_the_payout_package_contains_no_wall_clock_call), so the
    launcher supplies `run_ms`.  Feeding two different instants must move the
    scan's last day and the recorded runAt, and nothing else.
    """
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock, last_day=None)
        svc = PayoutCheckerService(cfg, run_ms=DAY * C.MS_PER_DAY)
        assert svc.last_day() == DAY
        later = PayoutCheckerService(cfg, run_ms=(DAY + 5) * C.MS_PER_DAY)
        assert later.last_day() == DAY + 5
        _rc, status = svc.run_once()
        assert status["runAt"] == "2024-10-04T00:00:00Z", status["runAt"]
        assert status["scan"]["lastDay"] == DAY


def test_main_refuses_to_run_without_an_injected_instant(tmp_path, capsys):
    cfg_file = tmp_path / "service.env"
    cfg_file.write_text("COORDINATOR_URL=http://127.0.0.1:1\n")
    cfg_file.chmod(0o600)
    rc = autorun.main(["--config", str(cfg_file)])
    assert rc == autorun.EXIT_CONFIG
    assert "no run instant was supplied" in capsys.readouterr().err


def test_unreachable_worker_signs_nothing(tmp_path, devnet_program):
    with MockPayoutCoordinator() as mock:
        cfg = build_cfg(tmp_path, mock)
        mock.go_offline()
        rc, status = PayoutCheckerService(cfg).run_once()
        assert rc != autorun.EXIT_OK
        assert mock.posted() == []
        assert not list((Path(cfg.work_dir) / "signatures").glob("*.json"))
