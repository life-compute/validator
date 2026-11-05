"""C47 §17 / C46 §6.3 — activation flags, anchor /2, duplicate ledger, pay.

These are the rules that decide WHEN the new behaviour applies, which is the
part that must not be got wrong: with both flags at 99999 every existing day
has to behave byte for byte as before.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from payout import constants as C
from payout import log_v2 as V2
from payout import validator_pay as VP
from payout.autorun import ServiceConfig, ServiceConfigError
from payout.chain_anchors import (
    IGNORED_WRONG_VERSION,
    LOG_V2_OFF,
    parse_any_anchor_memo,
    parse_anchor_memo,
    required_anchor_version,
)

PROGRAM_ID = "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"
SCORING_CONFIG = Path("/root/scratch/c47-spec/scoring_config.json")


# ════════════════════════════════════════════════════════════════════════════
# Flag parsing and ordering (task 4)
# ════════════════════════════════════════════════════════════════════════════
def test_the_defaults_are_off():
    assert C.LOG_V2_FROM_DAY_DEFAULT == 99999
    assert C.VALIDATOR_PAY_FROM_DAY_DEFAULT == 99999
    assert C.FLAG_OFF_DAY == 99999


@pytest.mark.parametrize("raw", ["0", "20000", " 20758 ", 20758])
def test_a_valid_flag_day_parses(raw):
    assert C.parse_flag_day(raw, "LOG_V2_FROM_DAY") == int(str(raw).strip())


@pytest.mark.parametrize("raw", ["", "  ", "x", "20.0", "-1", True, None, "1e3"])
def test_a_malformed_flag_day_is_refused(raw):
    with pytest.raises(ValueError):
        C.parse_flag_day(raw, "LOG_V2_FROM_DAY")


def test_both_flags_off_pass_validation():
    C.validate_flag_days(99999, 99999, genesis_day=20000)


def test_a_flag_below_the_genesis_day_is_refused():
    with pytest.raises(ValueError) as exc:
        C.validate_flag_days(19999, 99999, genesis_day=20000)
    assert "below GENESIS_DAY" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        C.validate_flag_days(20000, 19999, genesis_day=20000)
    assert "below GENESIS_DAY" in str(exc.value)


def test_validator_pay_below_log_v2_is_refused():
    with pytest.raises(ValueError) as exc:
        C.validate_flag_days(20800, 20799, genesis_day=20000)
    assert "below LOG_V2_FROM_DAY" in str(exc.value)
    # Equal is allowed: both start on the same day.
    C.validate_flag_days(20800, 20800, genesis_day=20000)
    C.validate_flag_days(20800, 20801, genesis_day=20000)


# ── the same rules, through the service configuration ──────────────────────
def _env(**over) -> dict[str, str]:
    base = {
        "COORDINATOR_URL": "https://example.invalid",
        "PROGRAM_ID": PROGRAM_ID,
        "LIFE_MINT": "FCwdMRVwxmrnftDgKC2qjkHThxRkYFUUEQieegUdKp3p",
        "TARGETS_TABLE": "/root/api/c5b-bundle/targets_table.json",
        "PAYOUT_KEYPAIR": "/root/keys/payout-online.json",
        "VALIDATOR_KEYPAIR": "/root/.life-compute/wallet.json",
        "ANCHOR_OPERATOR_PUBKEY": "11111111111111111111111111111112",
        "PAYOUT_GENESIS_DAY": "20757",
        "PAYOUT_FIRST_DAY": "20757",
    }
    base.update({k: v for k, v in over.items() if v is not None})
    for k, v in over.items():
        if v is None:
            base.pop(k, None)
    return base


def test_absent_flags_default_to_off():
    cfg = ServiceConfig.from_mapping(_env())
    assert cfg.log_v2_from_day == 99999
    assert cfg.validator_pay_from_day == 99999
    assert not cfg.log_v2_enabled
    assert not cfg.validator_pay_enabled


def test_the_explicit_off_values_are_accepted():
    cfg = ServiceConfig.from_mapping(
        _env(LOG_V2_FROM_DAY="99999", VALIDATOR_PAY_FROM_DAY="99999"))
    assert cfg.log_v2_from_day == 99999 and cfg.validator_pay_from_day == 99999


def test_a_flag_below_the_genesis_day_refuses_the_service():
    with pytest.raises(ServiceConfigError) as exc:
        ServiceConfig.from_mapping(_env(LOG_V2_FROM_DAY="20756"))
    assert "below GENESIS_DAY" in str(exc.value)


def test_validator_pay_below_log_v2_refuses_the_service():
    with pytest.raises(ServiceConfigError) as exc:
        ServiceConfig.from_mapping(
            _env(LOG_V2_FROM_DAY="20800", VALIDATOR_PAY_FROM_DAY="20799",
                 SCORING_CONFIG=str(SCORING_CONFIG)))
    assert "below LOG_V2_FROM_DAY" in str(exc.value)


def test_log_v2_without_a_scoring_config_refuses_the_service():
    with pytest.raises(ServiceConfigError) as exc:
        ServiceConfig.from_mapping(_env(LOG_V2_FROM_DAY="20800"))
    assert "SCORING_CONFIG" in str(exc.value)


def test_an_active_flag_reaches_the_checker_environment():
    cfg = ServiceConfig.from_mapping(
        _env(LOG_V2_FROM_DAY="20800", VALIDATOR_PAY_FROM_DAY="20800",
             SCORING_CONFIG=str(SCORING_CONFIG)))
    env = cfg.checker_env()
    assert env["LOG_V2_FROM_DAY"] == "20800"
    assert env["VALIDATOR_PAY_FROM_DAY"] == "20800"
    assert env["SCORING_CONFIG"] == str(SCORING_CONFIG)
    red = cfg.redacted()
    assert red["logV2FromDay"] == 20800 and red["logV2Enabled"] is True
    assert red["validatorPayFromDay"] == 20800


def test_the_banner_always_names_both_flags(tmp_path, monkeypatch):
    """Task 4: the settings must appear in the start-up banner of every run,
    active or not, and the loud line only when active."""
    from payout.autorun import PayoutCheckerService
    lines: list[str] = []

    cfg = ServiceConfig.from_mapping(
        _env(PAYOUT_WORK_DIR=str(tmp_path), PAYOUT_LAST_DAY="20757"))
    svc = PayoutCheckerService(cfg, run_ms=1_793_577_600_000,
                               log=lines.append)
    monkeypatch.setattr(svc, "discover", lambda: [])
    monkeypatch.setattr(svc, "chain_scan", lambda *a, **k: None)
    svc.run_once()
    banner = [l for l in lines if "activation flags:" in l]
    assert len(banner) == 1
    assert "LOG_V2_FROM_DAY=99999 (OFF)" in banner[0]
    assert "VALIDATOR_PAY_FROM_DAY=99999 (OFF)" in banner[0]
    assert not [l for l in lines if "LOG FORMAT V2 IS ACTIVE" in l]
    assert not [l for l in lines if "VALIDATOR PAY IS ACTIVE" in l]


def test_an_active_flag_prints_a_loud_line(tmp_path, monkeypatch):
    from payout.autorun import PayoutCheckerService
    lines: list[str] = []
    cfg = ServiceConfig.from_mapping(
        _env(PAYOUT_WORK_DIR=str(tmp_path), PAYOUT_LAST_DAY="20757",
             LOG_V2_FROM_DAY="20800", VALIDATOR_PAY_FROM_DAY="20800",
             SCORING_CONFIG=str(SCORING_CONFIG)))
    svc = PayoutCheckerService(cfg, run_ms=1_793_577_600_000,
                               log=lines.append)
    monkeypatch.setattr(svc, "discover", lambda: [])
    monkeypatch.setattr(svc, "chain_scan", lambda *a, **k: None)
    svc.run_once()
    assert [l for l in lines if "LOG FORMAT V2 IS ACTIVE" in l]
    assert [l for l in lines if "VALIDATOR PAY IS ACTIVE" in l]
    banner = [l for l in lines if "activation flags:" in l][0]
    assert "LOG_V2_FROM_DAY=20800 " in banner and "(OFF)" not in banner


# ════════════════════════════════════════════════════════════════════════════
# Anchor /2 (§17.4) — required version per day
# ════════════════════════════════════════════════════════════════════════════
def test_with_the_flag_off_every_day_requires_version_one():
    for day in (20000, 20757, 20758, 20762, 99998):
        assert required_anchor_version(day, LOG_V2_OFF) == 1
    # Only the flag day itself and above require /2.
    assert required_anchor_version(99999, LOG_V2_OFF) == 2


def test_the_flag_day_is_the_boundary():
    assert required_anchor_version(20799, 20800) == 1
    assert required_anchor_version(20800, 20800) == 2
    assert required_anchor_version(20801, 20800) == 2


def test_both_grammars_parse_and_report_their_version():
    v1 = (f"LIFE-ANCHOR/1 day=20757 date=2026-10-31 log={'a' * 64} "
          f"program={PROGRAM_ID}")
    v2 = (f"LIFE-ANCHOR/2 day=20764 date=2026-11-07 log={'a' * 64} "
          f"signed={'b' * 64} program={PROGRAM_ID}")
    assert parse_any_anchor_memo(v1)["version"] == 1
    assert parse_any_anchor_memo(v1)["signed_hash"] is None
    assert parse_any_anchor_memo(v2)["version"] == 2
    assert parse_any_anchor_memo(v2)["signed_hash"] == "b" * 64
    # The v1 parser never accepts a v2 memo and vice versa.
    assert parse_anchor_memo(v2) is None
    assert V2.parse_anchor_memo_v2(v1) is None


def test_a_v2_memo_on_a_flag_off_day_is_ignored_as_the_wrong_version():
    """The accept rule is unchanged; only the required VERSION differs, and a
    memo of the other version is an ignore reason, never an anchor."""
    from payout.chain_anchors import ChainAnchorBuilder

    operator = "11111111111111111111111111111112"
    tx = {
        "slot": 1,
        "meta": {"err": None},
        "transaction": {"message": {
            "accountKeys": [{"pubkey": operator, "signer": True}],
            "instructions": [{
                "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
                "parsed": (f"LIFE-ANCHOR/2 day=20757 date=2026-10-31 "
                           f"log={'a' * 64} signed={'b' * 64} "
                           f"program={PROGRAM_ID}")}]}},
    }
    off = ChainAnchorBuilder(PROGRAM_ID, operator, rpc=object(),
                             log_v2_from_day=LOG_V2_OFF)
    record, why = off._record_from_tx("sig", tx)
    assert record is None and why == IGNORED_WRONG_VERSION

    on = ChainAnchorBuilder(PROGRAM_ID, operator, rpc=object(),
                            log_v2_from_day=20757)
    record, why = on._record_from_tx("sig", tx)
    assert why is None and record is not None
    assert record.version == 2 and record.signed_hash == "b" * 64
    assert record.as_dict()["signedHash"] == "b" * 64


def test_a_v1_memo_on_a_v2_day_is_no_longer_sufficient():
    from payout.chain_anchors import ChainAnchorBuilder
    operator = "11111111111111111111111111111112"
    tx = {
        "slot": 1, "meta": {"err": None},
        "transaction": {"message": {
            "accountKeys": [{"pubkey": operator, "signer": True}],
            "instructions": [{
                "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
                "parsed": (f"LIFE-ANCHOR/1 day=20800 date=2026-12-13 "
                           f"log={'a' * 64} program={PROGRAM_ID}")}]}},
    }
    b = ChainAnchorBuilder(PROGRAM_ID, operator, rpc=object(),
                           log_v2_from_day=20800)
    record, why = b._record_from_tx("sig", tx)
    assert record is None and why == IGNORED_WRONG_VERSION


def test_the_same_transient_and_refusal_outcomes_still_apply():
    """A failed tx, a non-memo tx and the wrong program are still IGNORED for
    both versions — the accept rule did not change."""
    from payout.chain_anchors import (
        ChainAnchorBuilder, IGNORED_FAILED_TX, IGNORED_NO_MEMO,
        IGNORED_WRONG_PROGRAM, IGNORED_NOT_OPERATOR,
    )
    operator = "11111111111111111111111111111112"
    memo = (f"LIFE-ANCHOR/2 day=20757 date=2026-10-31 log={'a' * 64} "
            f"signed={'b' * 64} program={PROGRAM_ID}")
    b = ChainAnchorBuilder(PROGRAM_ID, operator, rpc=object(),
                           log_v2_from_day=20757)

    def tx(memo_text=memo, err=None, program=PROGRAM_ID, signer=operator,
           with_memo=True):
        ixs = ([{"programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
                 "parsed": memo_text.replace(PROGRAM_ID, program)}]
               if with_memo else [])
        return {"slot": 1, "meta": {"err": err},
                "transaction": {"message": {
                    "accountKeys": [{"pubkey": signer, "signer": True}],
                    "instructions": ixs}}}

    assert b._record_from_tx("s", tx(err="boom"))[1] == IGNORED_FAILED_TX
    assert b._record_from_tx("s", tx(with_memo=False))[1] == IGNORED_NO_MEMO
    assert b._record_from_tx(
        "s", tx(program="BU7WVCvy9CqypB7Hqhw6RJakAeSieVsWNapytQpFgi18")
    )[1] == IGNORED_WRONG_PROGRAM
    assert b._record_from_tx(
        "s", tx(signer="BU7WVCvy9CqypB7Hqhw6RJakAeSieVsWNapytQpFgi18")
    )[1] == IGNORED_NOT_OPERATOR


# ════════════════════════════════════════════════════════════════════════════
# The duplicate ledger — an ADDITION that must not break the existing one
# ════════════════════════════════════════════════════════════════════════════
def test_the_signed_request_ledger_is_a_separate_file(tmp_path):
    from payout.ledger import DecisionLedger

    old = DecisionLedger(tmp_path)
    old.record_decision({"verdict": "sign", "day": 20758, "batchId": "20758000"})
    old.mark_signed(20758000, [1, 2], day=20758, day_target_hits={"ESR1": 2})
    before_index = (tmp_path / "payout_index.json").read_bytes()
    before_log = (tmp_path / "payout_decisions.jsonl").read_bytes()

    led = V2.SignedRequestLedger(tmp_path)
    led.add_nonce("wallet1", "n1")
    led.add_work("wallet1", "mol1", "ESR1")
    led.save()

    assert (tmp_path / "signed_request_ledger.json").is_file()
    # The existing ledger files are untouched, byte for byte.
    assert (tmp_path / "payout_index.json").read_bytes() == before_index
    assert (tmp_path / "payout_decisions.jsonl").read_bytes() == before_log
    # …and the old ledger still reads correctly afterwards.
    reread = DecisionLedger(tmp_path)
    assert reread.signed_batch_ids == {20758000}
    assert reread.paid_result_ids == {1, 2}


def test_the_ledger_survives_a_restart(tmp_path):
    led = V2.SignedRequestLedger(tmp_path)
    led.add_nonce("w", "nonce-a")
    led.add_work("w", "mol", "ESR1")
    led.save()

    again = V2.SignedRequestLedger(tmp_path)
    assert again.has_nonce("w", "nonce-a")
    assert again.has_work("w", "mol", "ESR1")
    assert not again.has_nonce("w", "nonce-b")
    assert not again.has_work("w", "mol", "CDK4")


def test_a_second_day_replaying_a_nonce_is_refused(tmp_path):
    """The golden day, accepted once, is refused on a replay — that is what
    the cross-day ledger is for."""
    from payout.golden_v2 import GoldenVectors
    if not Path("/root/scratch/c47-spec/spec/golden/log_v2").is_dir():
        pytest.skip("golden vectors not present")

    g = GoldenVectors()
    led = V2.SignedRequestLedger(tmp_path)
    lines = [V2.SignedLine.from_json(s) for s in g.signed_raw]

    first = V2.evaluate_day(
        canonical_lines=g.canonical, signed_lines=lines,
        scoring_config=g.scoring_config, registry=g.registry,
        expected_canonical_hash=g.manifest["canonical_hash"],
        expected_signed_hash=g.manifest["signed_hash"],
        anchor_v2_memo=g.manifest["anchor_v2_memo"],
        expectations=g.expectations(), ledger=led)
    assert first.code == V2.ACCEPTED, first.detail

    V2.commit_day_to_ledger(lines, g.canonical, led)

    second = V2.evaluate_day(
        canonical_lines=g.canonical, signed_lines=lines,
        scoring_config=g.scoring_config, registry=g.registry,
        expected_canonical_hash=g.manifest["canonical_hash"],
        expected_signed_hash=g.manifest["signed_hash"],
        anchor_v2_memo=g.manifest["anchor_v2_memo"],
        expectations=g.expectations(), ledger=led)
    assert second.code == V2.DUPLICATE_SIGNED_REQUEST, second.detail


# ════════════════════════════════════════════════════════════════════════════
# Validator pay (§6.3-new), behind VALIDATOR_PAY_FROM_DAY
# ════════════════════════════════════════════════════════════════════════════
def test_the_rule_is_off_below_the_flag_day():
    assert not VP.validator_pay_active(20758, 99999)
    assert not VP.validator_pay_active(20799, 20800)
    assert VP.validator_pay_active(20800, 20800)
    assert VP.validator_pay_active(20801, 20800)


def test_the_legacy_fee_is_the_same_function_as_before():
    """§6.3-legacy must not move: it delegates to the existing constant."""
    for reward in (1, 300_000, 700_000, 900_000, 252_000, 12_345):
        assert VP.legacy_validator_fee(reward) == C.validator_fee(reward)
        assert VP.legacy_validator_fee(reward) == (reward * 750) // 10_000


def _cv(result_id, wallet, archived_at):
    return VP.ConfirmedVerdict(result_id, wallet, archived_at)


def test_the_first_k_validators_in_canonical_order_are_paid():
    confirmed = [
        _cv(1, "C" * 32, 300),       # third by archived_at
        _cv(1, "A" * 32, 100),       # first
        _cv(1, "B" * 32, 200),       # second
    ]
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=confirmed,
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: "M" * 32})
    assert plan.paid_validators[1] == ["A" * 32, "B" * 32]
    assert plan.amount_for(1, "C" * 32) is None


def test_each_paid_validator_gets_the_full_miner_amount():
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(1, "A" * 32, 100), _cv(1, "B" * 32, 200)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: "M" * 32})
    assert plan.amount_for(1, "A" * 32) == 700_000
    assert plan.amount_for(1, "B" * 32) == 700_000
    assert plan.total_raw == 1_400_000


def test_the_miner_is_excluded_before_the_k_cut():
    """Self-verification must not consume one of the K paid slots."""
    miner = "M" * 32
    plan = VP.build_validator_pay_plan(
        day=20800,
        confirmed=[_cv(1, miner, 50), _cv(1, "A" * 32, 100),
                   _cv(1, "B" * 32, 200)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: miner})
    assert plan.paid_validators[1] == ["A" * 32, "B" * 32]
    assert plan.self_excluded[1] == [miner]
    assert plan.amount_for(1, miner) is None


def test_one_summed_entry_per_wallet_with_roles():
    """A wallet that mines result 2 and validates result 1 gets ONE entry."""
    from payout.recompute import ComputedEntry, ComputedResult

    both = "11111111111111111111111111111112"
    other = "11111111111111111111111111111113"
    miner_entry = ComputedEntry(recipient=both, results=[ComputedResult(
        result_id=2, target="ESR1", tier="medium", molecule_hash="m",
        archived_at=200, amount_raw=700_000, validator_fee_raw=52_500,
        hit_count_before=0, base_reward=700_000, taper_num=4)])
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(1, both, 100)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: other})

    entries = VP.combine_entries([miner_entry], plan)
    assert len(entries) == 1
    e = entries[0]
    assert e.recipient == both
    assert e.miner_amount_raw == 700_000
    assert e.validator_amount_raw == 700_000
    assert e.amount_raw == 1_400_000
    assert sorted(r["role"] for r in e.results) == ["miner", "validator"]
    assert VP.total_raw(entries) == 1_400_000


def test_entries_are_sorted_by_pubkey_bytes_ascending():
    from payout.batch_hash import pubkey_bytes
    wallets = ["4K5sRsNJYvzXjUy9k5MceVgZo8CBUjnMzv44Q2sdPFGY",
               "HCpE72R34HXL6isgJbPTvr6MtVZxGP9ohnqfdCinMgct",
               "A6oFZC1eERz5TFygucCeNF6qFvEGqB1WLMktP99rD9J"]
    plan = VP.ValidatorPayPlan(day=20800, shares=[
        VP.ValidatorShare(i, w, "M" * 32, 1) for i, w in enumerate(wallets)])
    entries = VP.combine_entries([], plan)
    keys = [pubkey_bytes(e.recipient) for e in entries]
    assert all(keys[i] < keys[i + 1] for i in range(len(keys) - 1))


def test_a_result_the_recomputation_does_not_pay_pays_no_validator():
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(7, "A" * 32, 100)],
        miner_reward_by_result={},          # result 7 is not payable
        miner_wallet_by_result={7: "M" * 32})
    assert plan.shares == []
    assert 7 not in plan.paid_validators


# ── the batch-side checks ───────────────────────────────────────────────────
def _batch(entries):
    from payout.verifier import Batch
    return Batch(batch_id=20800000, day=20800, total_raw=0,
                 batch_hash="0" * 64, entries=entries, log_hash=None,
                 program_id=None, life_mint=None, raw={})


def test_validator_pay_mismatch_is_reported():
    from payout.verifier import verify_validator_pay
    wallet = "11111111111111111111111111111112"
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(1, wallet, 100)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: "11111111111111111111111111111113"})
    batch = _batch([{"recipient": wallet, "amountRaw": "350000",
                     "results": [{"resultId": 1, "role": "validator",
                                  "amountRaw": "350000"}]}])
    codes = [f.code for f in verify_validator_pay(batch, plan)]
    assert "validator_pay_mismatch" in codes


def test_validator_over_limit_is_reported():
    from payout.verifier import verify_validator_pay
    ws = ["11111111111111111111111111111112",
          "11111111111111111111111111111113",
          "11111111111111111111111111111114"]
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(1, w, 100 + i) for i, w in enumerate(ws)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: "11111111111111111111111111111115"})
    batch = _batch([{"recipient": w, "amountRaw": "700000",
                     "results": [{"resultId": 1, "role": "validator",
                                  "amountRaw": "700000"}]} for w in ws])
    codes = [f.code for f in verify_validator_pay(batch, plan)]
    assert "validator_over_limit" in codes


def test_validator_self_pay_is_reported():
    from payout.verifier import verify_validator_pay
    miner = "11111111111111111111111111111112"
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(1, miner, 100)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: miner})
    batch = _batch([{"recipient": miner, "amountRaw": "700000",
                     "results": [{"resultId": 1, "role": "validator",
                                  "amountRaw": "700000"}]}])
    codes = [f.code for f in verify_validator_pay(batch, plan)]
    assert "validator_self_pay" in codes


def test_validator_not_verified_is_reported():
    from payout.verifier import verify_validator_pay
    paid = "11111111111111111111111111111112"
    other = "11111111111111111111111111111113"
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(1, other, 100)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: "11111111111111111111111111111115"})
    batch = _batch([{"recipient": paid, "amountRaw": "700000",
                     "results": [{"resultId": 1, "role": "validator",
                                  "amountRaw": "700000"}]}])
    codes = [f.code for f in verify_validator_pay(batch, plan)]
    assert "validator_not_verified" in codes


def test_a_correct_validator_batch_has_no_findings():
    from payout.verifier import verify_validator_pay
    w = "11111111111111111111111111111112"
    plan = VP.build_validator_pay_plan(
        day=20800, confirmed=[_cv(1, w, 100)],
        miner_reward_by_result={1: 700_000},
        miner_wallet_by_result={1: "11111111111111111111111111111113"})
    batch = _batch([{"recipient": w, "amountRaw": "700000",
                     "results": [{"resultId": 1, "role": "validator",
                                  "amountRaw": "700000"}]}])
    assert verify_validator_pay(batch, plan) == []


# ════════════════════════════════════════════════════════════════════════════
# Edge days — the boundary is exactly "at or above"
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("flag,day,active", [
    (99999, 20758, False),
    (99999, 99998, False),
    (99999, 99999, True),
    (20800, 20799, False),
    (20800, 20800, True),
    (20800, 20801, True),
    (20000, 20000, True),
    (20000, 19999, False),
])
def test_the_activation_boundary_is_at_or_above(flag, day, active, tmp_path):
    from payout.service import PayoutSignerService
    svc = PayoutSignerService.__new__(PayoutSignerService)
    svc.log_v2_from_day = flag
    svc.validator_pay_from_day = flag
    assert svc.log_v2_active(day) is active
    assert svc.validator_pay_active(day) is active


# ════════════════════════════════════════════════════════════════════════════
# The no-wall-clock guard still holds over the NEW modules
# ════════════════════════════════════════════════════════════════════════════
def test_the_new_v2_modules_read_no_clock():
    """§2/C5j: the whole `payout` package must contain no wall-clock call.

    The existing guard walks every *.py in the package, so the new modules
    (log_v2, scoring_config, chain_state, validator_pay, golden_v2) are
    already covered — this asserts they are actually present in that walk, so
    a future rename cannot quietly drop one out of the check.
    """
    import tokenize
    import payout
    pkg_dir = Path(payout.__file__).parent
    names = {p.name for p in pkg_dir.glob("*.py")}
    for new in ("log_v2.py", "scoring_config.py", "chain_state.py",
                "validator_pay.py", "golden_v2.py"):
        assert new in names, f"{new} is not in the no-clock walk"

    banned = ("NOW_MS", "now_ms", "time.time", "time_ns", "datetime.now",
              "utcnow", "date.today", "monotonic", "perf_counter")
    offenders = {}
    for src in sorted(pkg_dir.glob("*.py")):
        toks = []
        with src.open("rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                toks.append(tok.string)
        code = "".join(toks)
        hits = [b for b in banned if b.replace(".", "") in code.replace(".", "")]
        if hits:
            offenders[src.name] = hits
    assert not offenders, f"wall-clock references: {offenders}"
