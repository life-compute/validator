"""C47 §17 — the log-format-v2 checker, against the real golden vectors.

Everything here runs offline: the golden files are the real ones generated
through the Worker's own `closeDay()` with real Ed25519 throwaway keys, and
registration is answered from manifest.json's `registered_wallets`, which is
what the golden README prescribes for these synthetic wallets.

The table this produces is the one quoted in the report.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from payout import log_v2 as V2
from payout.golden_v2 import DEFAULT_GOLDEN_DIR, DEFAULT_SCORING_CONFIG, GoldenVectors
from payout.scoring_config import SCORING_CONFIG_SHA256, ScoringConfig

pytestmark = pytest.mark.skipif(
    not DEFAULT_GOLDEN_DIR.is_dir(),
    reason="the C47 golden vectors are not present on this machine")


@pytest.fixture(scope="module")
def golden_v2() -> GoldenVectors:
    return GoldenVectors()


# ── the pinned scoring config (§17.5) ───────────────────────────────────────
def test_scoring_config_pin_is_the_published_value():
    raw = Path(DEFAULT_SCORING_CONFIG).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SCORING_CONFIG_SHA256
    cfg = ScoringConfig.from_file(DEFAULT_SCORING_CONFIG)
    assert cfg.coord_config_version == "2.0.0"
    assert cfg.has_version("2.0.0")
    assert not cfg.has_version("9.9.9")


def test_a_drifted_scoring_config_is_refused(tmp_path):
    """§17.5: a config whose bytes hash to anything else must not be used."""
    from payout.errors import PayoutConfigError
    raw = Path(DEFAULT_SCORING_CONFIG).read_bytes()
    bad = tmp_path / "scoring_config.json"
    bad.write_bytes(raw + b"\n")        # one byte is enough
    with pytest.raises(PayoutConfigError) as exc:
        ScoringConfig.from_file(bad)
    assert "does not match the pinned value" in str(exc.value)


def test_null_and_absent_thresholds_are_always_a_hit():
    cfg = ScoringConfig.from_file(DEFAULT_SCORING_CONFIG)
    consts = cfg.constants("2.0.0")
    assert consts.hit_threshold("ESR1") is None      # explicit null
    assert consts.hit_threshold("NOT_IN_THE_MAP") is None   # absent
    assert consts.hit_threshold("CDK4") == -9.0
    assert consts.tolerance("protein", False) == 0.35
    assert consts.tolerance("protein", True) == 0.25


# ── the positive vector ─────────────────────────────────────────────────────
def test_the_golden_positive_vector_is_accepted(golden_v2):
    result = golden_v2.positive()
    assert result.code == V2.ACCEPTED, result.detail
    # Three signed lines → three assembled logical records.
    assert len(result.records) == 3


def test_the_assembled_record_has_the_fourteen_fields(golden_v2):
    rec = golden_v2.positive().records[0].as_dict()
    assert sorted(rec) == sorted([
        "result_id", "wallet", "target", "molecule_hash", "archived_at",
        "coord_score", "validator_wallet", "validator_score",
        "coord_decision", "hit_status", "config_version", "bias_factor",
        "miner_req", "verdict_req"])
    # The first six come from canonical.ndjson…
    assert rec["result_id"] == 1
    assert rec["target"] == "ESR1"
    assert rec["coord_score"] == -6.864
    # …validator_score is decoded from the SIGNED verdict body…
    assert rec["validator_score"] == -6.741
    # …and the decision/hit are recomputed, not read.
    assert rec["coord_decision"] == "confirm"
    assert rec["hit_status"] == "confirmed_hit"
    assert rec["config_version"] == "2.0.0"
    assert rec["bias_factor"] == 1.0


def test_the_request_digests_match_the_manifests_recorded_values(golden_v2):
    """§17.3: miner_req / verdict_req = sha256(signed_msg + "\\n" + sig)."""
    by_key = {(r.result_id, r.validator_wallet): r
              for r in golden_v2.positive().records}
    for result in golden_v2.manifest["results"]:
        for v in result["validators"]:
            rec = by_key[(result["result_id"], v["validator_wallet"])]
            assert rec.miner_req == v["miner_req_digest"]
            assert rec.verdict_req == v["verdict_req_digest"]


def test_signed_ndjson_reserialises_byte_for_byte(golden_v2):
    """`signed=` commits to exact bytes, so the re-serialisation must match."""
    on_disk = (DEFAULT_GOLDEN_DIR / "signed.ndjson").read_bytes()
    lines = [V2.SignedLine.from_json(s) for s in golden_v2.signed_raw]
    assert V2.signed_ndjson_bytes(lines) == on_disk
    expected = (DEFAULT_GOLDEN_DIR / "signed.sha256").read_text().strip()
    assert V2.signed_hash_hex(on_disk) == expected
    assert expected == golden_v2.manifest["signed_hash"]


def test_day_number_and_date_agree_by_the_formula(golden_v2):
    """§17.3: day = floor(archived_at / 86_400_000), date = day_to_iso(day)."""
    from payout.batch_hash import day_to_iso
    archived = golden_v2.canonical[0]["archived_at"]
    assert V2.day_from_archived_at(archived) == 20764
    assert day_to_iso(20764) == "2026-11-07"
    assert golden_v2.manifest["day_num"] == 20764
    assert golden_v2.manifest["day"] == "2026-11-07"


# ── all 18 mutations, each with its own expected code ───────────────────────
def _mutation_ids():
    if not DEFAULT_GOLDEN_DIR.is_dir():
        return []
    return [(m["id"], m["code"], m["file"])
            for m in json.loads(
                (DEFAULT_GOLDEN_DIR / "negative_mutations.json").read_text())]


@pytest.mark.parametrize("mid,expected,path", _mutation_ids())
def test_each_mutation_gives_exactly_its_expected_code(golden_v2, mid, expected, path):
    result = golden_v2.mutation(path)
    assert result.code == expected, (
        f"{mid}: expected {expected}, got {result.code} ({result.detail})")


def test_every_mutation_file_carries_its_own_expected_code():
    """The index and the files must agree — a drifted pair would make the
    parametrised run above check the wrong thing."""
    index = json.loads(
        (DEFAULT_GOLDEN_DIR / "negative_mutations.json").read_text())
    assert len(index) == 18
    for m in index:
        data = json.loads((DEFAULT_GOLDEN_DIR / m["file"]).read_text())
        assert data["id"] == m["id"]
        assert data["code"] == m["code"]


def test_the_full_table_is_nineteen_of_nineteen(golden_v2):
    cases = golden_v2.run_all()
    assert len(cases) == 19
    failed = [(c.name, c.expected, c.got, c.detail) for c in cases if not c.passed]
    assert not failed, failed


# ── refusal-code convention (§17.6b) ────────────────────────────────────────
def test_every_refusal_code_is_lower_snake_case():
    import re
    codes = [v for k, v in vars(V2).items()
             if k.isupper() and isinstance(v, str) and k != "ACCEPTED"
             and "_" in k and not k.endswith(("_S", "_RESULT", "_DAY"))]
    for code in codes:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", code):
            continue    # non-code constants (e.g. file names) are skipped
        assert code == code.lower()
    # the named ones, explicitly
    for code in (V2.MINER_SIG_INVALID, V2.VALIDATOR_SIG_INVALID,
                 V2.SCORING_CONFIG_UNKNOWN, V2.VALIDATOR_SELF_VERIFY,
                 V2.ANCHOR_V2_MISMATCH, V2.DUPLICATE_SIGNED_REQUEST):
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code), code


# ── the anchor /2 grammar (§17.4) ───────────────────────────────────────────
def test_anchor_v2_memo_parses_exactly(golden_v2):
    memo = golden_v2.manifest["anchor_v2_memo"]
    parsed = V2.parse_anchor_memo_v2(memo)
    assert parsed == {
        "day": 20764, "date": "2026-11-07",
        "log_hash": golden_v2.manifest["canonical_hash"],
        "signed_hash": golden_v2.manifest["signed_hash"],
        "program_id": "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"}


@pytest.mark.parametrize("memo", [
    "",
    "hello",
    # a /1 memo is not a /2 memo
    "LIFE-ANCHOR/1 day=20764 date=2026-11-07 log=" + "a" * 64
    + " program=HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv",
    # signed= missing
    "LIFE-ANCHOR/2 day=20764 date=2026-11-07 log=" + "a" * 64
    + " program=HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv",
    # wrong hash length
    "LIFE-ANCHOR/2 day=20764 date=2026-11-07 log=" + "a" * 64
    + " signed=" + "b" * 63
    + " program=HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv",
    # fields out of order
    "LIFE-ANCHOR/2 day=20764 date=2026-11-07 signed=" + "b" * 64
    + " log=" + "a" * 64
    + " program=HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv",
    # trailing junk
    "LIFE-ANCHOR/2 day=20764 date=2026-11-07 log=" + "a" * 64
    + " signed=" + "b" * 64
    + " program=HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv extra",
])
def test_a_memo_that_is_not_exactly_the_v2_grammar_is_not_an_anchor(memo):
    assert V2.parse_anchor_memo_v2(memo) is None
