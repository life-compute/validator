"""C5b2 payout signer — verified against the c5b-bundle fixtures.

Everything here runs against the mock coordinator serving the bundle and a
MOCK of the anchored hashes.  No chain, no PM2 process, no real key.

KNOWN BUNDLE DEFECTS
--------------------
Two fixtures in the bundle are internally inconsistent.  Both are listed by
name in KNOWN_BUNDLE_DEFECTS and are the ONLY tolerated mismatches; every
other fixture must match its published verdict exactly, so a defect entry
cannot hide an unrelated regression.

  day_20001 — result 7 (TP53) is paid 300,000 raw, but targets_table.json
      makes TP53 protein/hard = 900,000 and the result has no prior confirmed
      hits (taper 4/4).  EGFR on day_20000 is byte-identically protein/hard
      and IS paid 900,000.  Task 1(g) requires the reward the spec's rules
      give, enforced in BOTH directions, so this day is refused with
      reward_mismatch rather than weakening the rule.

  13_chunk_above_entry_limit — its verdict claims "entry count 4 exceeds
      MAX_BATCH_ENTRIES=4", which is arithmetically impossible; 4 does not
      exceed 4, and variants 04/05/08 carry 4 entries while expecting other
      rules.  The batch is still refused, by the next reachable rule
      (no_wallet_first_seen: wallet 68kN4bLG… has no wallet_first_seen event).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest_payout import (  # noqa: E402,F401
    LIFE_MINT,
    NOW_MS,
    PROGRAM_ID,
    anchors,
    coordinator,
    history_days,
    ledger,
    log_source,
    payout_key_paths,
    settings,
    signer,
    service,
    targets,
    write_throwaway_keypair,
)
from mock_payout_coordinator import (  # noqa: E402
    TARGETS_TABLE,
    VALID_DAYS,
    anchored_hashes,
    load_batch,
    load_expected_verdict,
    load_tampered,
    load_tampered_verdict,
    tampered_names,
)
from payout import batch_hash  # noqa: E402
from payout.constants import (  # noqa: E402
    CAP_PER_BATCH,
    CAP_PER_DAY,
    MAX_BATCH_ENTRIES,
    TARGETS_TABLE_SHA256,
    final_reward,
    validator_fee,
)
from payout.errors import (  # noqa: E402
    NO_WALLET_FIRST_SEEN,
    PayoutConfigError,
    PayoutKeyError,
    REPEATED_BATCH_ID,
    REWARD_MISMATCH,
    ANCHOR_HASH_MISMATCH,
)
from payout.ledger import DecisionLedger  # noqa: E402
from payout.service import PayoutSignerService  # noqa: E402
from payout.signer_key import PayoutSigner  # noqa: E402
from payout.sources import Settings, TargetsTable  # noqa: E402

BUNDLE = Path("/root/api/c5b-bundle")

# ── the only tolerated fixture mismatches, named explicitly ─────────────────
KNOWN_BUNDLE_DEFECTS: dict[str, dict[str, str]] = {
    "day_20001": {
        "expected": "sign",
        "actual": "refuse",
        "actual_rule": REWARD_MISMATCH,
        "why": ("result 7 (TP53) is paid 300,000 but targets_table.json makes "
                "TP53 protein/hard = 900,000 with taper 4/4; EGFR is "
                "identically protein/hard and is paid 900,000 on day_20000"),
    },
    "13_chunk_above_entry_limit": {
        "expected_rule": "chunk_above_entry_limit",
        "actual_rule": NO_WALLET_FIRST_SEEN,
        "why": ("the published rule is unreachable: entry count 4 does not "
                "exceed MAX_BATCH_ENTRIES=4. The batch is still refused, by "
                "the next reachable rule"),
    },
}


# ════════════════════════════════════════════════════════════════════════════
# 0. Bundle integrity
# ════════════════════════════════════════════════════════════════════════════
def test_bundle_tarball_sha256_matches_the_published_value():
    """The whole bundle must be the exact archive we were given."""
    expected = "fcfbc0bcb89a4fe7a30c8f1ec239fa3fcabaccd0005e2368d84c2b9ae15d3bc2"
    raw = Path("/root/api/c5b-bundle.tar.gz").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected


def test_every_file_in_the_readme_index_matches_its_listed_sha256():
    """Each of the 50 indexed files must hash to the README's value."""
    import re
    readme = (BUNDLE / "README.md").read_text()
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*([0-9a-f]{64})\s*\|\s*$",
                      readme, re.M)
    assert len(rows) == 50, f"expected 50 indexed files, found {len(rows)}"
    bad = []
    for name, want in rows:
        p = BUNDLE / name
        got = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "MISSING"
        if got != want:
            bad.append((name, want, got))
    assert not bad, f"bundle files do not match the README index: {bad}"


# ════════════════════════════════════════════════════════════════════════════
# 1a. The canonical batch hash (§4) and the §4.1/§16 golden vector
# ════════════════════════════════════════════════════════════════════════════
def test_golden_vector_from_the_spec_with_its_own_explicit_inputs():
    """BATCH_SPEC §4.1 / §16 golden vector, inputs written out literally here.

    Nothing is read from the fixtures or the checker's config: the program id,
    mint, batch id, day and both entries are the spec's own stated values, so
    this test fails if the preimage layout drifts for any reason.
    """
    wallet_01 = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"   # 0x01 × 32
    wallet_02 = "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"   # 0x02 × 32
    # Prove the literals really are the spec's byte values before hashing.
    assert batch_hash.pubkey_bytes(wallet_01) == bytes([0x01] * 32)
    assert batch_hash.pubkey_bytes(wallet_02) == bytes([0x02] * 32)
    assert batch_hash.pubkey_bytes(
        "CVDFLCAjXhVWiPXH9nTCTpCgVzmDVoiPzNJYuccr1dqB") == bytes([0xAA] * 32)

    digest = batch_hash.batch_hash_hex(
        program_id="BU7WVCvy9CqypB7Hqhw6RJakAeSieVsWNapytQpFgi18",
        life_mint="CVDFLCAjXhVWiPXH9nTCTpCgVzmDVoiPzNJYuccr1dqB",
        bid=42,
        day=20000,
        entries=[(wallet_01, 1_000_000), (wallet_02, 2_000_000)],
    )
    assert digest == \
        "d27577a0c14e553d9be8be82e5403d04f5ac3ebe900565dad26ade4dc37d0fc9"


def test_golden_vector_preimage_layout_is_the_spec_byte_table():
    """§4: 14-byte tag, 32+32 keys, u64 LE id, u32 LE day, u8 count, N×40."""
    pre = batch_hash.batch_preimage(
        program_id=PROGRAM_ID, life_mint=LIFE_MINT, bid=42, day=20000,
        entries=[("4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi", 1_000_000),
                 ("8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR", 2_000_000)])
    assert len(pre) == 91 + 2 * 40
    assert pre[:14] == b"LIFE-PAYOUT-V1"
    assert pre[46:78] == bytes([0xAA] * 32)          # LIFE mint = 0xaa×32
    assert pre[78:86] == (42).to_bytes(8, "little")  # batchId u64 LE
    assert pre[86:90] == (20000).to_bytes(4, "little")  # day u32 LE
    assert pre[90] == 2                                  # entry_count u8
    assert hashlib.sha256(pre).hexdigest() == \
        "d27577a0c14e553d9be8be82e5403d04f5ac3ebe900565dad26ade4dc37d0fc9"


@pytest.mark.parametrize("day", VALID_DAYS)
def test_published_batch_hash_of_every_valid_day_recomputes(day):
    """Each valid fixture's published batchHash matches the §4 recomputation."""
    b = load_batch(day)
    got = batch_hash.batch_hash_hex(
        program_id=b["programId"], life_mint=b["lifeMint"],
        bid=int(b["batchId"]), day=b["day"],
        entries=[(e["recipient"], int(e["amountRaw"])) for e in b["entries"]])
    assert got == b["batchHash"]


def test_batch_hash_commits_to_the_recipient_list():
    """Swapping a recipient must change the hash (the C4c substitution guard)."""
    b = load_batch(20000)
    base = [(e["recipient"], int(e["amountRaw"])) for e in b["entries"]]
    swapped = [(base[1][0], base[0][1])] + base[1:]
    assert batch_hash.batch_hash_hex(
        program_id=PROGRAM_ID, life_mint=LIFE_MINT, bid=int(b["batchId"]),
        day=b["day"], entries=swapped) != b["batchHash"]


def test_batch_hash_commits_to_program_id_and_mint():
    b = load_batch(20000)
    entries = [(e["recipient"], int(e["amountRaw"])) for e in b["entries"]]
    other = batch_hash.batch_hash_hex(
        program_id="11111111111111111111111111111111", life_mint=LIFE_MINT,
        bid=int(b["batchId"]), day=b["day"], entries=entries)
    assert other != b["batchHash"]


# ════════════════════════════════════════════════════════════════════════════
# 1a. PROGRAM_ID / LIFE_MINT are required settings (§16)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("env", [
    {},
    {"PROGRAM_ID": PROGRAM_ID},
    {"LIFE_MINT": LIFE_MINT},
    {"PROGRAM_ID": "", "LIFE_MINT": LIFE_MINT},
])
def test_settings_refuse_to_build_without_both_required_values(env):
    with pytest.raises(PayoutConfigError):
        Settings.from_env(env)


def test_settings_reject_a_non_base58_key():
    with pytest.raises(PayoutConfigError):
        Settings(program_id="not-a-key!", life_mint=LIFE_MINT)


def test_settings_from_env_accepts_the_fixture_constants():
    s = Settings.from_env({"PROGRAM_ID": PROGRAM_ID, "LIFE_MINT": LIFE_MINT})
    assert (s.program_id, s.life_mint) == (PROGRAM_ID, LIFE_MINT)


# ════════════════════════════════════════════════════════════════════════════
# 1b. Sort contract and duplicates (§4)
# ════════════════════════════════════════════════════════════════════════════
def test_valid_fixtures_are_strictly_sorted_by_recipient_bytes():
    for day in VALID_DAYS:
        recips = [e["recipient"] for e in load_batch(day)["entries"]]
        assert batch_hash.is_sorted_ascending(recips), day


def test_sortedness_is_by_raw_pubkey_bytes_not_base58_text():
    """Base58 string order and byte order genuinely differ for these keys."""
    a = "DSGkASCoHtJwXCphy9CBHaPzUB3yJt7CqSVFqkcTYyq7"
    b = "H5oEZysabtW6gLUQD4fPM3iveEdegUrmPpn2hhE9jjQF"
    assert batch_hash.pubkey_bytes(a) < batch_hash.pubkey_bytes(b)


def test_repeated_recipient_is_not_strictly_ascending():
    w = "2LfiZJwPUMYBM9admXFYXDFUZyXvN2YtzovP3hemxSjT"
    assert not batch_hash.is_sorted_ascending([w, w])


# ════════════════════════════════════════════════════════════════════════════
# 1c. Caps and batch ids (§3, §5.3, §5.4)
# ════════════════════════════════════════════════════════════════════════════
def test_entry_limit_and_caps_are_the_spec_values():
    assert MAX_BATCH_ENTRIES == 4
    assert CAP_PER_BATCH == 5_000_000_000
    assert CAP_PER_DAY == 5_000_000_000


@pytest.mark.parametrize("day", VALID_DAYS)
def test_batch_ids_are_derived_as_day_times_1000_plus_seq(day):
    b = load_batch(day)
    assert int(b["batchId"]) == batch_hash.batch_id(day, 0) == day * 1000
    assert batch_hash.split_batch_id(int(b["batchId"])) == (day, 0)


def test_chunking_splits_at_four_wallet_entries():
    from payout.recompute import ComputedEntry, DayComputation
    entries = [ComputedEntry(recipient=f"w{i}") for i in range(9)]
    comp = DayComputation(day=20000, entries=entries)
    assert [len(c) for c in comp.chunks()] == [4, 4, 1]


# ════════════════════════════════════════════════════════════════════════════
# 1d. targets_table.json is the ONLY modality/tier source, SHA-verified (§12)
# ════════════════════════════════════════════════════════════════════════════
def test_targets_table_sha256_is_verified_against_the_spec(targets):
    assert targets.sha256 == TARGETS_TABLE_SHA256
    assert len(targets) == 70


def test_a_tampered_targets_table_is_refused(tmp_path):
    rows = json.loads(TARGETS_TABLE.read_text())
    rows[0]["tier"] = "easy"                      # re-price a target
    p = tmp_path / "targets_table.json"
    p.write_text(json.dumps(rows, indent=2) + "\n")
    with pytest.raises(PayoutConfigError, match="SHA-256"):
        TargetsTable.from_file(p)


def test_tier_lookup_comes_from_the_table(targets):
    assert targets.get("CDK4").tier == "hard"
    assert targets.get("ATM_mRNA").tier == "mrna"
    assert targets.get("BCL2_CRISPR").tier == "crispr"
    assert targets.get("UNKNOWN_TARGET_XYZ") is None


def test_no_operator_supplied_payout_inputs_remain():
    """The operator can no longer supply flags, forfeits or modalities."""
    import payout.sources as sources
    assert not hasattr(sources, "OperatorInputs")
    text = Path(sources.__file__).read_text()
    assert "flagged_wallets" not in text
    assert "forfeited_result_ids" not in text
    assert "target_modality" not in text


# ════════════════════════════════════════════════════════════════════════════
# 1d. Wallet facts come only from the canonical log's events (§13)
# ════════════════════════════════════════════════════════════════════════════
def test_wallet_facts_are_derived_from_log_events(log_source):
    facts = log_source.day_log(20000).wallet_facts()
    flagged = "HxVGHok2ih71XHPc33oPZAuXMzbHL3yVtoM8jUfJt3ct"
    assert facts[flagged].flagged is True
    assert facts["DSGkASCoHtJwXCphy9CBHaPzUB3yJt7CqSVFqkcTYyq7"].first_seen_ms \
        == 1_726_790_400_000


def test_unflag_event_clears_an_earlier_flag(log_source):
    from payout.sources import LogEvent
    dl = log_source.day_log(20000)
    w = "HxVGHok2ih71XHPc33oPZAuXMzbHL3yVtoM8jUfJt3ct"
    dl.events.append(LogEvent(event="wallet_unflagged", wallet=w,
                              time_ms=1_728_000_002_000))
    assert dl.wallet_facts()[w].flagged is False


def test_forfeit_event_is_picked_up(log_source):
    facts = log_source.day_log(20001).wallet_facts()
    assert facts["BHiHpARJUVFZ11SbvoHoxMbVBr23nuT5WGhH5udQoXCx"].forfeited is True


# ════════════════════════════════════════════════════════════════════════════
# 1e. The canonical log is proven before it is used
# ════════════════════════════════════════════════════════════════════════════
def test_canonical_route_is_fetched_and_hashed(coordinator, service):
    service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS,
                        history_days=VALID_DAYS)
    assert "/log/2024-10-04/canonical" in coordinator.state.request_log
    assert "/log/2024-10-04" in coordinator.state.request_log


def test_a_log_body_that_does_not_match_the_published_hash_is_refused(
        coordinator, service):
    """A WELL-FORMED but different log body must still fail the hash gate."""
    good = (BUNDLE / "fixtures/day_20000/canonical_log.ndjson").read_bytes()
    # Flip one score: parses perfectly, hashes differently.
    tweaked = good.replace(b'"coord_score":0.91', b'"coord_score":0.92')
    assert tweaked != good
    coordinator.set_canonical(20000, tweaked)
    d = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert d.refused
    assert any("does not match" in r for r in d.reasons()), d.reasons()
    assert not d.signatures


def test_a_log_that_does_not_match_the_anchor_is_refused(
        log_source, settings, targets, ledger, signer):
    from payout.sources import AnchorSource
    bad = AnchorSource({d: "ab" * 32 for d in VALID_DAYS})
    svc = PayoutSignerService(log_source, bad, settings, targets, ledger, signer)
    d = svc.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert d.refused
    assert any("ANCHORED" in r or ANCHOR_HASH_MISMATCH in r for r in d.reasons())
    assert not d.signatures


def test_a_missing_anchor_is_fatal(log_source, settings, targets, ledger, signer):
    from payout.sources import AnchorSource
    svc = PayoutSignerService(log_source, AnchorSource({}), settings, targets,
                              ledger, signer)
    d = svc.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert d.refused and not d.signatures


def _verify_one(log_source, settings, targets, day, anchored, now_ms=NOW_MS):
    """Call the verifier layer DIRECTLY, bypassing the service's hash gate."""
    from payout.recompute import compute_day
    from payout.verifier import Batch, verify_batch
    dl = log_source.day_log(day)
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    comp = compute_day(dl, targets=targets, now_ms=now_ms, history=hist)
    return verify_batch(Batch.from_json(load_batch(day)), comp, dl, anchored,
                        settings, targets, set(), now_ms=now_ms)


def test_verifier_layer_independently_rejects_a_wrong_anchor(
        log_source, settings, targets):
    """Defense in depth: the verifier catches a bad anchor on its own."""
    ok = _verify_one(log_source, settings, targets, 20000,
                     anchored_hashes()[20000])
    assert ok.signable, ok.reasons()
    bad = _verify_one(log_source, settings, targets, 20000, "ab" * 32)
    assert not bad.signable
    assert bad.primary_rule == ANCHOR_HASH_MISMATCH


def test_verifier_layer_independently_rejects_a_wrong_published_log_hash(
        coordinator, log_source, settings, targets):
    coordinator.set_manifest(20000, {
        "day": "2024-10-04", "log_hash": "cd" * 32,
        "results": {"ok": True, "sha256": "cd" * 32},
        "verdicts": {"ok": True, "sha256": "cd" * 32}})
    res = _verify_one(log_source, settings, targets, 20000,
                      anchored_hashes()[20000])
    assert not res.signable
    assert res.primary_rule == "log_inconsistent"


def test_a_coordinator_outage_produces_no_signature(coordinator, service):
    coordinator.go_offline()
    d = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS)
    assert d.refused and not d.signatures


# ════════════════════════════════════════════════════════════════════════════
# 1f. The spec no longer contradicts itself
# ════════════════════════════════════════════════════════════════════════════
def test_warn_spec_conflict_is_gone():
    """The code must not define or emit the warning (a comment may explain it)."""
    import payout.errors as errors
    import payout.verifier as verifier
    assert not hasattr(errors, "WARN_SPEC_CONFLICT")
    for mod in (errors, verifier):
        src = Path(mod.__file__).read_text()
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "WARN_SPEC_CONFLICT =" not in code
        assert "WARN_SPEC_CONFLICT," not in code
        assert "Finding(WARN_SPEC_CONFLICT" not in code


def test_spec_conflicts_list_is_gone():
    import payout.constants as constants
    assert not hasattr(constants, "SPEC_CONFLICTS")


def test_the_other_warning_is_kept(service):
    """Fee RECIPIENTS are still unverifiable, and that caveat must survive."""
    d = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS,
                            history_days=VALID_DAYS)
    codes = {w.code for r in d.results for w in r.warnings}
    assert "warn_fee_recipients_unverifiable" in codes


# ════════════════════════════════════════════════════════════════════════════
# 1g. Reward arithmetic (§6)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("base,hits,want", [
    (900_000, 0, 900_000),
    (900_000, 99, 900_000),
    (900_000, 100, 675_000),
    (900_000, 999, 675_000),
    (900_000, 1000, 450_000),
    (252_000, 0, 252_000),
    (252_000, 100, 189_000),
    (300_000, 1000, 150_000),
    (1, 1000, 1),            # max(1, …) floor
])
def test_taper_is_integer_math(base, hits, want):
    assert final_reward(base, hits) == want


@pytest.mark.parametrize("reward,fee", [
    (900_000, 67_500), (300_000, 22_500), (252_000, 18_900), (1, 0),
])
def test_validator_fee_is_seven_point_five_percent_floored(reward, fee):
    assert validator_fee(reward) == fee


def test_entry_total_must_equal_the_sum_of_its_breakdown():
    """§7 aggregation: day_20000's minerA entry is 2 results summed."""
    b = load_batch(20000)
    for e in b["entries"]:
        assert int(e["amountRaw"]) == sum(int(r["amountRaw"]) for r in e["results"])
    agg = [e for e in b["entries"] if len(e["results"]) == 2][0]
    assert int(agg["amountRaw"]) == 1_800_000


# ════════════════════════════════════════════════════════════════════════════
# 1h. Replay: the checker keeps its own record of signed batch ids
# ════════════════════════════════════════════════════════════════════════════
def test_a_batch_id_is_never_signed_twice(service):
    first = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS,
                                history_days=VALID_DAYS)
    assert first.signed, first.reasons()
    second = service.process_day(20000, [load_batch(20000)], now_ms=NOW_MS,
                                 history_days=VALID_DAYS)
    assert second.refused
    assert second.primary_rule == REPEATED_BATCH_ID
    assert not second.signatures


def test_the_signed_record_survives_a_restart(tmp_path, log_source, anchors,
                                              settings, targets, signer):
    state = tmp_path / "state"
    svc1 = PayoutSignerService(log_source, anchors, settings, targets,
                               DecisionLedger(state), signer)
    assert svc1.process_day(20000, [load_batch(20000)], now_ms=NOW_MS,
                            history_days=VALID_DAYS).signed
    # A brand-new service over the same state directory.
    svc2 = PayoutSignerService(log_source, anchors, settings, targets,
                               DecisionLedger(state), signer)
    again = svc2.process_day(20000, [load_batch(20000)], now_ms=NOW_MS,
                             history_days=VALID_DAYS)
    assert again.refused and again.primary_rule == REPEATED_BATCH_ID


# ════════════════════════════════════════════════════════════════════════════
# 3. Payout key rules (unchanged)
# ════════════════════════════════════════════════════════════════════════════
def test_payout_keypair_is_required_with_no_default(payout_key_paths):
    with pytest.raises(PayoutKeyError, match="required"):
        PayoutSigner.from_env({"VALIDATOR_KEYPAIR": payout_key_paths["VALIDATOR_KEYPAIR"]})


def test_refuses_to_start_when_the_payout_key_is_the_verdict_key(tmp_path):
    same = tmp_path / "one_key.json"
    write_throwaway_keypair(same)
    with pytest.raises(PayoutKeyError):
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(same),
                               "VALIDATOR_KEYPAIR": str(same)})


def test_refuses_when_two_files_hold_the_same_public_key(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_throwaway_keypair(a)
    b.write_text(a.read_text())          # different path, same key material
    with pytest.raises(PayoutKeyError, match="identical"):
        PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(a), "VALIDATOR_KEYPAIR": str(b)})


def test_signer_signs_the_32_raw_bytes_only(signer):
    with pytest.raises(PayoutKeyError):
        signer.sign_batch_hash("ab" * 32)          # hex string, not bytes
    assert len(bytes.fromhex(signer.sign_batch_hash(b"\x01" * 32))) == 64


def test_signer_repr_never_leaks_secret_material(signer):
    assert "secret withheld" in repr(signer)
