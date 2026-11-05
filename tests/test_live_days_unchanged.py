"""The LIVE days 20758..20761 still verify exactly as before, flags OFF.

This is the regression gate for C47: adding log format v2 and validator pay
must not move a single day the checker has already decided.  The four days
the live service signed are replayed here from SAVED FIXTURES — the real
`GET /log/{date}`, `/log/{date}/canonical` and `/payout/{date}` responses,
recorded read-only from the devnet Worker — and checked against the hashes
and verdicts recorded in the live ledger at
`/root/scratch/validator_c2/payout_state/payout_decisions.jsonl`.

RECORDED VALUES (from the live run, before this change):

    day    batchId    totalRaw   batchHash (first 16)  verdict
    20758  20758000     700000   aef82bd3634038e2      sign
    20759  20759000    4200000   f840f5f85df0bb9b      sign
    20760  20760000    3500000   40ac45a1f5bcbfda      sign
    20761  20761000    4200000   10292a7a8dfe3349      sign

The signature is Ed25519 over the 32 raw bytes of batchHash, so an identical
batchHash is an identical signature: no key file is opened here.

Nothing in this file touches the network, a key, or a PM2 process.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from payout import constants as C
from payout.batch_hash import batch_hash_hex, day_to_iso, log_hash_hex
from payout.ledger import DecisionLedger
from payout.recompute import compute_day
from payout.service import PayoutSignerService
from payout.sources import AnchorSource, DayLog, LogSource, Settings, TargetsTable
from payout.verifier import Batch, verify_day

DATA = Path(__file__).parent / "data" / "live_days_20757_20761.json"
TARGETS = Path("/root/api/c5b-bundle/targets_table.json")

PROGRAM_ID = "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"
LIFE_MINT = "FCwdMRVwxmrnftDgKC2qjkHThxRkYFUUEQieegUdKp3p"
GENESIS_DAY = 20757

# The anchored log hashes as published on chain (payout-work/anchors.json).
LIVE_ANCHORS = {
    20757: "60ce558188ee2abd32a62d139161b5fcabb6a31fd5f032d0acae319fd2970b25",
    20758: "f31c505a4ac189878155798ab4509fa369ddae63850ba29d7fba186d435deda7",
    20759: "126ee75bf8c8e37595392626cdd35ae0f8fe7d6d304baa7c917ea9471f197837",
    20760: "43c4ca027e6b232ff9b20e0e7517c6d5aed2e90c6c7b2a828d18b8d022c57e9c",
    20761: "b1986bbce26b14dc64523e8c07d00fd7d63a9b77d368d046779c9794fc6d5143",
}

# What the live checker recorded for each day, BEFORE this change.
RECORDED = {
    20758: {"batchId": 20758000, "totalRaw": 700_000,
            "batchHash": "aef82bd3634038e2"},
    20759: {"batchId": 20759000, "totalRaw": 4_200_000,
            "batchHash": "f840f5f85df0bb9b"},
    20760: {"batchId": 20760000, "totalRaw": 3_500_000,
            "batchHash": "40ac45a1f5bcbfda"},
    20761: {"batchId": 20761000, "totalRaw": 4_200_000,
            "batchHash": "10292a7a8dfe3349"},
}

pytestmark = pytest.mark.skipif(
    not (DATA.is_file() and TARGETS.is_file()),
    reason="the recorded live-day fixtures are not present")


@pytest.fixture(scope="module")
def live() -> dict:
    return json.loads(DATA.read_text())["days"]


class _FixtureLogSource(LogSource):
    """A LogSource that serves the recorded bytes and makes no request."""

    def __init__(self, days: dict):
        super().__init__("http://fixture.invalid", strict_closed=True)
        self._days = days

    def _get(self, path):            # pragma: no cover - must never be hit
        raise AssertionError(f"the fixture source made a real request: {path}")

    def log_manifest(self, day: int) -> dict:
        return self._days[str(int(day))]["manifest"]

    def canonical_log_bytes(self, day: int) -> bytes:
        return self._days[str(int(day))]["canonical"].encode("utf-8")

    def signed_log_bytes(self, day: int) -> bytes:   # pragma: no cover
        raise AssertionError(
            "signed.ndjson must NOT be fetched while LOG_V2_FROM_DAY is off")


def _service(live, tmp_path, **over) -> PayoutSignerService:
    kwargs = dict(log_v2_from_day=C.LOG_V2_FROM_DAY_DEFAULT,
                  validator_pay_from_day=C.VALIDATOR_PAY_FROM_DAY_DEFAULT)
    kwargs.update(over)
    return PayoutSignerService(
        _FixtureLogSource(live), AnchorSource(LIVE_ANCHORS),
        Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT, holdback_days=0),
        TargetsTable.from_file(TARGETS), DecisionLedger(tmp_path),
        signer=None, **kwargs)


# ── the recorded log hashes still recompute ─────────────────────────────────
@pytest.mark.parametrize("day", sorted(LIVE_ANCHORS))
def test_each_live_day_hashes_to_its_anchored_value(live, day):
    raw = live[str(day)]["canonical"].encode("utf-8")
    assert log_hash_hex(raw) == LIVE_ANCHORS[day]
    assert live[str(day)]["manifest"]["log_hash"] == LIVE_ANCHORS[day]
    assert live[str(day)]["manifest"]["closed"] is True


# ── the recorded batch hashes still recompute ───────────────────────────────
@pytest.mark.parametrize("day", sorted(RECORDED))
def test_each_live_batch_hash_is_unchanged(live, day):
    chunks = live[str(day)]["payout"]["chunks"]
    assert len(chunks) == 1
    batch = Batch.from_json(chunks[0])
    assert batch.batch_id == RECORDED[day]["batchId"]
    assert batch.total_raw == RECORDED[day]["totalRaw"]
    recomputed = batch_hash_hex(program_id=PROGRAM_ID, life_mint=LIFE_MINT,
                                bid=batch.batch_id, day=batch.day,
                                entries=batch.entry_pairs())
    assert recomputed.startswith(RECORDED[day]["batchHash"]), (
        f"day {day}: batch hash moved — recomputed {recomputed[:16]}, "
        f"recorded {RECORDED[day]['batchHash']}")
    # The envelope's own claim must agree too (it is never trusted, only
    # compared).
    assert batch.batch_hash == recomputed


# ── the whole pipeline still signs each day, with the flags off ─────────────
@pytest.mark.parametrize("day", sorted(RECORDED))
def test_each_live_day_still_verifies_clean(live, tmp_path, day):
    svc = _service(live, tmp_path / str(day))
    assert not svc.log_v2_active(day), "v2 must be OFF for a live day"
    assert not svc.validator_pay_active(day)

    chunks = live[str(day)]["payout"]["chunks"]
    decision = svc.process_day(day, chunks, genesis_day=GENESIS_DAY)

    # No payout key is loaded here, so the ONLY finding allowed is the
    # "verification passed but no key" one — every payout rule must pass.
    rules = {f.code for r in decision.results for f in r.findings}
    assert rules in ({"no_payout_key"}, set()), (
        f"day {day} now fails rules it did not fail before: {rules} / "
        f"{decision.reasons()}")
    assert decision.results[0].batch_hash_recomputed.startswith(
        RECORDED[day]["batchHash"])
    assert decision.results[0].total_raw == RECORDED[day]["totalRaw"]


def test_the_v2_path_is_never_entered_with_the_flag_off(live, tmp_path):
    """_FixtureLogSource.signed_log_bytes raises if it is ever called, so a
    clean run of all four days proves signed.ndjson was never fetched."""
    for day in sorted(RECORDED):
        svc = _service(live, tmp_path / f"off{day}")
        svc.process_day(day, live[str(day)]["payout"]["chunks"],
                        genesis_day=GENESIS_DAY)


def test_turning_the_flag_on_for_a_v1_day_refuses_it(live, tmp_path):
    """The other direction: a day with no signed.ndjson must be REFUSED once
    it is at or above LOG_V2_FROM_DAY — not silently accepted."""
    from payout.scoring_config import ScoringConfig
    cfg_path = Path("/root/scratch/c47-spec/scoring_config.json")
    if not cfg_path.is_file():
        pytest.skip("scoring_config.json not present")

    class _NoSigned(_FixtureLogSource):
        def signed_log_bytes(self, day: int) -> bytes:
            from payout.errors import CoordinatorUnreachable
            raise CoordinatorUnreachable("GET /log/{date}/signed returned 404")

    svc = PayoutSignerService(
        _NoSigned(live), AnchorSource(LIVE_ANCHORS),
        Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT, holdback_days=0),
        TargetsTable.from_file(TARGETS), DecisionLedger(tmp_path / "on"),
        signer=None, log_v2_from_day=20758, validator_pay_from_day=20758,
        scoring_config=ScoringConfig.from_file(cfg_path),
        chain_registry=None)
    decision = svc.process_day(20758, live["20758"]["payout"]["chunks"],
                               genesis_day=GENESIS_DAY)
    assert not decision.signed
    rules = {f.code for r in decision.results for f in r.findings}
    assert "log_v2_refused" in rules, rules


# ── the legacy 7.5% fee is still the rule for these days ───────────────────
@pytest.mark.parametrize("day", sorted(RECORDED))
def test_the_legacy_validator_fee_rule_applies_to_the_live_days(live, tmp_path, day):
    from payout import validator_pay as VP
    assert not VP.validator_pay_active(day, C.VALIDATOR_PAY_FROM_DAY_DEFAULT)
    # Every per-result fee the batch carries is still floor(reward * 7.5%).
    for chunk in live[str(day)]["payout"]["chunks"]:
        for ent in chunk.get("entries", []):
            for r in ent.get("results", []):
                # Pre-C47 batches carry no `role` field at all.
                assert "role" not in r, (
                    "a legacy-rule day must not carry §6.3-new role fields")
                for fee in (r.get("validatorFees") or []):
                    assert int(fee["amountRaw"]) == C.validator_fee(
                        int(r["amountRaw"]))
