"""Payout signer — golden days, negative controls, key separation, replay.

Everything runs against the mock coordinator that serves the C5b bundle
fixtures plus a mock of the anchored hashes.  No chain, no live coordinator,
no PM2 process, no real keypair.

Fixtures come from conftest_payout; importing them into this module's
namespace is how pytest picks them up without a non-rootdir pytest_plugins
declaration (removed in pytest 8+).
"""
from __future__ import annotations

import json

import pytest

from conftest_payout import (  # noqa: F401
    anchors, coordinator, FLAGGED_WALLET, ledger, log_source, NOW_MS,
    operator, payout_key_paths, service, signer, write_throwaway_keypair,
)
from mock_payout_coordinator import (
    anchored_hashes, load_batch, load_expected_verdict, load_tampered,
    load_tampered_verdict,
)
from payout import constants as C
from payout.batch_hash import batch_hash_hex, batch_id, log_hash_hex
from payout.errors import PayoutKeyError
from payout.signer_key import PayoutSigner
from payout.verifier import Batch

GOLDEN_DAYS = (20000, 20001, 20002)


def codes(decision) -> set[str]:
    return {f.code for r in decision.results for f in r.findings}


# ══════════════════════════════════════════════════════════════════════════
# 0. Spec primitives
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("day", GOLDEN_DAYS)
def test_batch_hash_matches_fixture(day):
    """§4: SHA-256(batchId_le8 || day_le4 || totalRaw_le8) reproduces the fixture."""
    b = load_batch(day)
    assert batch_hash_hex(int(b["batchId"]), b["day"], int(b["totalRaw"])) == b["batchHash"]


@pytest.mark.parametrize("day", GOLDEN_DAYS)
def test_batch_id_derivation(day):
    """§3: batchId = day * 1000 + seq, seq=0 for these single-chunk days."""
    assert int(load_batch(day)["batchId"]) == batch_id(day, 0)


@pytest.mark.parametrize("day", GOLDEN_DAYS)
def test_canonical_log_hash_matches_anchor(day):
    """§9: the canonical log's SHA-256 equals the anchored hash fixture."""
    from mock_payout_coordinator import day_dir
    raw = (day_dir(day) / "canonical_log.ndjson").read_bytes()
    assert log_hash_hex(raw) == anchored_hashes()[day]


def test_reward_math_is_integer_and_tapered():
    """§6.2/§6.3 exact integer arithmetic, no floats."""
    assert C.final_reward(900_000, 0) == 900_000        # 4/4
    assert C.final_reward(900_000, 100) == 675_000      # 3/4
    assert C.final_reward(900_000, 1000) == 450_000     # 2/4
    assert C.final_reward(252_000, 999) == 189_000
    assert C.final_reward(1, 1000) == 1                 # max(1, ...) floor guard
    assert C.validator_fee(900_000) == 67_500           # 7.5%
    assert C.validator_fee(300_000) == 22_500
    assert C.validator_fee(1) == 0                      # floor, not round


# ══════════════════════════════════════════════════════════════════════════
# 1. Golden days: recompute from the public log and sign
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("day", GOLDEN_DAYS)
def test_golden_day_is_signed(service, day):
    """Each fixture day recomputes exactly and gets a signature."""
    decision = service.process_day(day, [load_batch(day)], now_ms=NOW_MS)
    assert decision.signed, decision.reasons()
    expected = load_expected_verdict(day)
    assert expected["verdict"] == "sign"
    assert len(decision.results) == 1
    res = decision.results[0]
    assert res.total_raw == int(expected["total_raw"])
    assert len(res.expected_entries) == expected["entry_count"]
    assert int(load_batch(day)["batchId"]) in decision.signatures


@pytest.mark.parametrize("day", GOLDEN_DAYS)
def test_signature_is_over_raw_batch_hash(service, signer, day):
    """§8: the signature verifies against the 32 RAW bytes of the batch hash."""
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    import base58

    from payout.batch_hash import batch_hash_bytes

    b = load_batch(day)
    decision = service.process_day(day, [b], now_ms=NOW_MS)
    assert decision.signed, decision.reasons()
    sig_hex = decision.signatures[int(b["batchId"])]
    msg = batch_hash_bytes(int(b["batchId"]), b["day"], int(b["totalRaw"]))
    assert len(msg) == 32
    sig = Signature.from_bytes(bytes.fromhex(sig_hex))
    assert sig.verify(Pubkey.from_bytes(base58.b58decode(signer.pubkey)), msg)
    # and NOT over the hex string
    assert not sig.verify(Pubkey.from_bytes(base58.b58decode(signer.pubkey)),
                          msg.hex().encode())


def test_recomputed_amounts_match_entry_by_entry(service):
    """Every fixture amount equals our independently computed reward."""
    b = load_batch(20000)
    decision = service.process_day(20000, [b], now_ms=NOW_MS)
    computed = {e.result_id: e.amount_raw for e in decision.results[0].expected_entries}
    given = {int(e["resultId"]): int(e["amountRaw"]) for e in b["entries"]}
    assert computed == given
    fees = {int(e["resultId"]): int(e["validatorFees"][0]["amountRaw"]) for e in b["entries"]}
    assert fees == {rid: C.validator_fee(amt) for rid, amt in computed.items()}


def test_day_20002_skips_the_non_confirmed_result(service):
    """Fixture expects result 8 skipped: it is not in the canonical log at all."""
    decision = service.process_day(20002, [load_batch(20002)], now_ms=NOW_MS)
    assert decision.signed, decision.reasons()
    paid = {e.result_id for e in decision.results[0].expected_entries}
    assert paid == {7}
    assert 8 in load_expected_verdict(20002)["skipped_ids"]


# ══════════════════════════════════════════════════════════════════════════
# 2. NEGATIVE CONTROLS — every one must refuse and produce no signature
# ══════════════════════════════════════════════════════════════════════════

def test_refuses_wallet_with_no_confirmed_result(service):
    """Negative control: a wallet/result with no confirmed result is refused."""
    decision = service.process_day(20000, [load_tampered("1_no_confirmed_result")],
                                   now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "NO_CONFIRMED_RESULT" in codes(decision)
    assert load_tampered_verdict("1_no_confirmed_result")["verdict"] == "refuse"
    assert any("9999" in r for r in decision.reasons())


def test_refuses_inflated_amount(service):
    """Negative control: an inflated amountRaw is refused."""
    decision = service.process_day(20000, [load_tampered("2_inflated_amount")],
                                   now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "ENTRY_AMOUNT_MISMATCH" in codes(decision)
    assert any("expected 900000" in r and "1800000" in r for r in decision.reasons())


def test_refuses_duplicate_batch_id(service):
    """Negative control: the same batchId is never signed twice."""
    first = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert first.signed, first.reasons()

    replay = service.process_day(20000, [load_tampered("3_repeated_batch_id")],
                                 now_ms=NOW_MS)
    assert not replay.signed
    assert replay.signatures == {}
    assert "BATCH_ID_ALREADY_SIGNED" in codes(replay)
    assert load_tampered_verdict("3_repeated_batch_id")["verdict"] == "refuse"


def test_refuses_entry_count_above_limit(service):
    """Negative control: >MAX_BATCH_ENTRIES entries in one chunk is refused."""
    for name in ("4_total_above_cap", "8_chunk_above_entry_limit"):
        decision = service.process_day(20000, [load_tampered(name)], now_ms=NOW_MS)
        assert not decision.signed, name
        assert decision.signatures == {}
        assert "ENTRY_COUNT_ABOVE_LIMIT" in codes(decision), name
        assert any(f"MAX_BATCH_ENTRIES={C.MAX_BATCH_ENTRIES}" in r
                   for r in decision.reasons()), name


def test_refuses_batch_total_above_cap(service):
    """Negative control: totalRaw above CAP_PER_BATCH is refused."""
    decision = service.process_day(20000, [load_tampered("7_batch_total_above_cap")],
                                   now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "BATCH_TOTAL_ABOVE_CAP" in codes(decision)
    assert any(str(C.CAP_PER_BATCH) in r for r in decision.reasons())


def test_refuses_day_total_above_cap(service):
    """Negative control: chunks that together breach CAP_PER_DAY are refused."""
    decision = service.process_day(20000, [load_tampered("9_chunks_exceed_day_cap")],
                                   now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "DAY_TOTAL_ABOVE_CAP" in codes(decision)


def test_refuses_tampered_log_day(service):
    """Negative control: a log day whose hash does not match is refused."""
    decision = service.process_day(20000, [load_tampered("5_log_hash_mismatch")],
                                   now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "LOG_HASH_MISMATCH" in codes(decision)
    assert load_tampered_verdict("5_log_hash_mismatch")["verdict"] == "refuse"


def test_refuses_when_anchored_hash_disagrees(log_source, operator, ledger, signer):
    """Negative control: log body that disagrees with the ANCHOR is refused.

    Distinct from the batch's own logHash field: here the batch and the served
    log agree, but the anchored (on-chain) hash does not.
    """
    from payout.service import PayoutSignerService
    from payout.sources import AnchorSource

    bad_anchor = AnchorSource({20000: "b" * 64})
    svc = PayoutSignerService(log_source, bad_anchor, operator, ledger, signer)
    decision = svc.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "ANCHOR_HASH_MISMATCH" in codes(decision)


def test_refuses_flagged_wallet(service):
    """Negative control: a flagged recipient is refused."""
    decision = service.process_day(20000, [load_tampered("6_flagged_holdback_wallet")],
                                   now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert {"WALLET_FLAGGED", "NO_CONFIRMED_RESULT"} & codes(decision)


def test_coordinator_outage_produces_no_signature(coordinator, service):
    """Negative control: an unreachable coordinator yields NO signature."""
    coordinator.go_offline()
    decision = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert decision.error is not None
    assert "COORDINATOR_UNREACHABLE" in decision.error
    # and it recovers cleanly once the coordinator is back
    coordinator.go_online()
    again = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert again.signed, again.reasons()


def test_anchor_source_unreachable_produces_no_signature(
        log_source, operator, ledger, signer):
    """Negative control: a missing anchor entry is fatal, never assumed OK."""
    from payout.service import PayoutSignerService
    from payout.sources import AnchorSource

    svc = PayoutSignerService(log_source, AnchorSource({}), operator, ledger, signer)
    decision = svc.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "no anchored log hash" in (decision.error or "")


def test_tampered_batch_hash_is_refused(service):
    """A batchHash that does not match the preimage is refused."""
    b = load_batch(20000)
    b["batchHash"] = "f" * 64
    decision = service.process_day(20000, [b], now_ms=NOW_MS)
    assert not decision.signed
    assert "BATCH_HASH_MISMATCH" in codes(decision)


def test_missing_payable_entry_is_refused(service):
    """Under-paying (dropping a payable result) is refused, not just over-paying."""
    b = load_batch(20000)
    dropped = b["entries"].pop()
    b["totalRaw"] = str(int(b["totalRaw"]) - int(dropped["amountRaw"]))
    b["batchHash"] = batch_hash_hex(int(b["batchId"]), b["day"], int(b["totalRaw"]))
    decision = service.process_day(20000, [b], now_ms=NOW_MS)
    assert not decision.signed
    assert "ENTRY_SET_MISMATCH" in codes(decision)


def test_total_not_sum_of_entries_is_refused(service):
    """totalRaw must equal the sum of miner amounts (§4)."""
    b = load_batch(20000)
    b["totalRaw"] = str(int(b["totalRaw"]) + 1)
    b["batchHash"] = batch_hash_hex(int(b["batchId"]), b["day"], int(b["totalRaw"]))
    decision = service.process_day(20000, [b], now_ms=NOW_MS)
    assert not decision.signed
    assert "TOTAL_NOT_SUM_OF_ENTRIES" in codes(decision)


def test_recipient_substitution_is_refused(service):
    """Swapping the payee while keeping the amount is refused."""
    b = load_batch(20000)
    b["entries"][0]["recipient"] = FLAGGED_WALLET
    decision = service.process_day(20000, [b], now_ms=NOW_MS)
    assert not decision.signed
    assert {"ENTRY_RECIPIENT_MISMATCH", "WALLET_FLAGGED"} & codes(decision)


def test_validator_fee_inflation_is_refused(service):
    """A fee above 7.5% is refused even when the miner amount is correct."""
    b = load_batch(20000)
    b["entries"][0]["validatorFees"][0]["amountRaw"] = "90000"
    decision = service.process_day(20000, [b], now_ms=NOW_MS)
    assert not decision.signed
    assert "VALIDATOR_FEE_MISMATCH" in codes(decision)


def test_unknown_target_modality_is_refused(log_source, anchors, ledger, signer):
    """Spec silent on a target's modality -> refuse, never guess (§6.1)."""
    from payout.service import PayoutSignerService
    from payout.sources import OperatorInputs

    thin = OperatorInputs(target_modality={"CDK4": "protein"},
                          flagged_wallets=set(), forfeited_result_ids=set(),
                          source_path="<thin fixture>")
    svc = PayoutSignerService(log_source, anchors, thin, ledger, signer)
    decision = svc.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "UNKNOWN_TARGET_MODALITY" in codes(decision)


def test_forfeited_result_is_refused(log_source, anchors, ledger, signer):
    """A forfeited result (§5.1.5) must not be paid."""
    from payout.service import PayoutSignerService
    from payout.sources import OperatorInputs
    from conftest_payout import OPERATOR_TARGET_MODALITY

    op = OperatorInputs(target_modality=dict(OPERATOR_TARGET_MODALITY),
                        flagged_wallets={FLAGGED_WALLET},
                        forfeited_result_ids={1}, source_path="<forfeit fixture>")
    svc = PayoutSignerService(log_source, anchors, op, ledger, signer)
    decision = svc.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert not decision.signed
    assert "ENTRY_SET_MISMATCH" in codes(decision)


def test_result_paid_twice_across_days_is_refused(service):
    """A result already in paid_results cannot be paid again (§5.1.6, §10)."""
    first = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert first.signed, first.reasons()

    replay = load_batch(20000)
    replay["batchId"] = str(batch_id(20000, 1))
    replay["batchHash"] = batch_hash_hex(int(replay["batchId"]), replay["day"],
                                         int(replay["totalRaw"]))
    decision = service.process_day(20000, [replay], now_ms=NOW_MS)
    assert not decision.signed
    assert "RESULT_ALREADY_PAID" in codes(decision)


# ══════════════════════════════════════════════════════════════════════════
# 3. Whole-day chunking
# ══════════════════════════════════════════════════════════════════════════

def test_duplicate_result_across_chunks_is_refused(service):
    """§5.4/§10: no result may be paid twice across a day's chunks."""
    b0 = load_batch(20000)
    b1 = json.loads(json.dumps(b0))
    b1["batchId"] = str(batch_id(20000, 1))
    b1["batchHash"] = batch_hash_hex(int(b1["batchId"]), b1["day"], int(b1["totalRaw"]))
    decision = service.process_day(20000, [b0, b1], now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "DUPLICATE_RESULT_ACROSS_CHUNKS" in codes(decision)


def test_one_bad_chunk_blocks_the_whole_day(service):
    """A day is signed all-or-nothing: no chunk is signed if any chunk fails."""
    good = load_batch(20000)
    bad = load_tampered("2_inflated_amount")
    bad["batchId"] = str(batch_id(20000, 1))
    bad["batchHash"] = batch_hash_hex(int(bad["batchId"]), bad["day"],
                                      int(bad["totalRaw"]))
    decision = service.process_day(20000, [good, bad], now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert all(not r.signable for r in decision.results)


# ══════════════════════════════════════════════════════════════════════════
# 4. Key separation and start-up guards
# ══════════════════════════════════════════════════════════════════════════

def test_payout_keypair_is_required_with_no_default():
    """PAYOUT_KEYPAIR has no default; unset -> refuse to start."""
    with pytest.raises(PayoutKeyError, match="required and has no default"):
        PayoutSigner.from_env({"VALIDATOR_KEYPAIR": "/nonexistent"})
    with pytest.raises(PayoutKeyError, match="required and has no default"):
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": "   ",
                               "VALIDATOR_KEYPAIR": "/nonexistent"})


def test_refuses_to_start_when_payout_key_equals_verdict_key(tmp_path):
    """Same public key for both roles -> refuse to start."""
    shared = tmp_path / "shared_throwaway.json"
    copy = tmp_path / "copy_throwaway.json"
    write_throwaway_keypair(shared)
    copy.write_text(shared.read_text())           # same key, different file

    with pytest.raises(PayoutKeyError, match="identical to the validator"):
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(copy),
                               "VALIDATOR_KEYPAIR": str(shared)})

    with pytest.raises(PayoutKeyError, match="same file"):
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(shared),
                               "VALIDATOR_KEYPAIR": str(shared)})


def test_distinct_keys_start_and_differ(payout_key_paths):
    """Two distinct throwaway keys start cleanly and have different pubkeys."""
    from payout.signer_key import validator_pubkey
    s = PayoutSigner.from_env(payout_key_paths)
    assert s.pubkey != validator_pubkey(payout_key_paths["VALIDATOR_KEYPAIR"])


def test_signer_never_leaks_secret_material(signer, payout_key_paths):
    """repr/str expose only a truncated PUBLIC key."""
    secret_bytes = json.loads(open(payout_key_paths["PAYOUT_KEYPAIR"]).read())
    seed_hex = bytes(secret_bytes[:32]).hex()
    for text in (repr(signer), str(signer), f"{signer}"):
        assert "secret withheld" in text
        assert seed_hex not in text
        assert signer.pubkey not in text          # truncated, never the full key


def test_signs_only_32_raw_bytes(signer):
    """The signer refuses anything that is not a 32-byte digest (§8)."""
    with pytest.raises(PayoutKeyError, match="32 raw bytes"):
        signer.sign_batch_hash(b"\x00" * 31)
    with pytest.raises(PayoutKeyError, match="32 raw bytes"):
        signer.sign_batch_hash(("a" * 64).encode())   # the hex string


def test_verification_without_a_key_produces_no_signature(
        log_source, anchors, operator, ledger):
    """No payout key loaded -> verification may pass, but nothing is signed."""
    from payout.service import PayoutSignerService
    svc = PayoutSignerService(log_source, anchors, operator, ledger, signer=None)
    decision = svc.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert not decision.signed
    assert decision.signatures == {}
    assert "NO_PAYOUT_KEY" in codes(decision)


# ══════════════════════════════════════════════════════════════════════════
# 5. Decision log and human summary
# ══════════════════════════════════════════════════════════════════════════

def test_every_decision_is_logged_with_differences(service, ledger):
    """Both the sign and the refuse path append a full record."""
    service.process_day(20001, [load_batch(20001)], now_ms=NOW_MS)
    service.process_day(20000, [load_tampered("2_inflated_amount")], now_ms=NOW_MS)

    rows = list(ledger.decisions())
    assert [r["verdict"] for r in rows] == ["sign", "refuse"]
    assert rows[0]["signature"] and len(rows[0]["signature"]) == 128
    assert rows[0]["findings"] == []
    assert rows[1]["signature"] is None
    assert any(f["code"] == "ENTRY_AMOUNT_MISMATCH" for f in rows[1]["findings"])
    assert all("1800000" not in json.dumps(r.get("payoutPubkey")) for r in rows)


def test_ledger_survives_a_restart(service, ledger, log_source, anchors,
                                   operator, signer):
    """The replay guard is durable: a fresh service still refuses the batchId."""
    from payout.ledger import DecisionLedger
    from payout.service import PayoutSignerService

    assert service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS).signed

    reopened = DecisionLedger(ledger.state_dir)
    assert reopened.was_signed(batch_id(20000, 0))
    svc2 = PayoutSignerService(log_source, anchors, operator, reopened, signer)
    decision = svc2.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert not decision.signed
    assert "BATCH_ID_ALREADY_SIGNED" in codes(decision)


def test_summary_is_plain_language_with_totals_and_changes(service):
    """The review sheet carries totals, top recipients and the day-over-day diff."""
    first = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert first.signed, first.reasons()
    second = service.process_day(20001, [load_batch(20001)], now_ms=NOW_MS)
    assert second.signed, second.reasons()

    s = second.summary
    assert "PAYOUT REVIEW — day 20001" in s
    assert "READY TO SIGN" in s
    assert "1.2 LIFE" in s                       # 1,200,000 raw miner total
    assert "Largest recipients" in s
    assert "3inyBSmRZr4TAaurejtPeRVNZoEqJULQkmXMihBbgaC8" in s
    assert "Change from the previous signed day" in s
    assert "Previous signed day: 20000" in s
    assert "3.6 LIFE" in s                       # previous day's total
    assert "Validator fees" in s and "NOT part of the batch total" in s


def test_refusal_summary_explains_every_difference(service):
    """A refused day tells the reviewer exactly what differed."""
    decision = service.process_day(20000, [load_tampered("2_inflated_amount")],
                                   now_ms=NOW_MS)
    s = decision.summary
    assert "REFUSED — DO NOT SIGN" in s
    assert "WHY THIS IS REFUSED" in s
    assert "ENTRY_AMOUNT_MISMATCH" in s
    assert "expected 900000" in s and "1800000" in s


def test_summary_records_the_spec_conflict(service):
    """The §12 vs §5.4 contradiction is surfaced, not silently resolved."""
    decision = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert "MAX_BATCH_ENTRIES=20" in decision.summary
    assert "stricter value 5 is enforced" in decision.summary
