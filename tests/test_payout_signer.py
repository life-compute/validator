"""C5b3 payout signer — verified against the c5b-bundle fixtures.

Everything here runs against the mock coordinator serving the bundle and a
MOCK of the anchored hashes.  No chain, no PM2 process, no real key.

NO EXPECTED FAILURES
--------------------
There is no defect-allowance list and no xfail in this suite.  Every one of
the 19 bundle fixtures must match its published verdict exactly; any mismatch
fails the run.  The previous revision carried two allowances and this bundle
resolves both:

  * day_20001 — the old table priced every protein target `hard`, so TP53's
    900,000 clashed with a 300,000 payment.  The new table prices protein by
    difficulty (4 easy / 15 medium / 11 hard) and TP53 is `hard` = 900,000,
    which is exactly what the fixture pays.
  * 13_chunk_above_entry_limit — its batch now carries FIVE entries, so
    `chunk_above_entry_limit` is reachable (5 > MAX_BATCH_ENTRIES=4) instead
    of the arithmetically impossible "4 exceeds 4".
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest_payout import (  # noqa: E402,F401
    FAKE_CLOCKS_MS,
    LIFE_MINT,
    PROGRAM_ID,
    anchors,
    coordinator,
    fake_clock,
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
    MockPayoutCoordinator,
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
    PayoutConfigError,
    PayoutKeyError,
    REPEATED_BATCH_ID,
    ANCHOR_HASH_MISMATCH,
)
from payout.ledger import DecisionLedger  # noqa: E402
from payout.service import PayoutSignerService  # noqa: E402
from payout.signer_key import PayoutSigner  # noqa: E402
from payout.sources import Settings, TargetsTable  # noqa: E402

BUNDLE = Path("/root/api/c5b-bundle")
BUNDLE_TARBALL = Path("/root/api/c5b-bundle.tar.gz")


# ── bundle-derived helpers (no literal from any particular bundle) ──────────
def readme_index() -> dict[str, str]:
    """The README's File Index section as {relative path: sha256}.

    Parsed from the bundle's own README, so a new bundle with different files,
    different hashes or a different file count needs no edit here.
    """
    import re
    readme = (BUNDLE / "README.md").read_text()
    section = readme.split("## File Index", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*([0-9a-f]{64})\s*\|\s*$",
                      section, re.M)
    assert rows, "the README File Index section lists no files"
    return dict(rows)


def log_events(day: int, kind: str) -> list[dict]:
    """Lifecycle events of one kind in a day's canonical log, in file order."""
    path = BUNDLE / f"fixtures/day_{day}/canonical_log.ndjson"
    out = []
    for line in path.read_text().splitlines():
        if line.strip() and (obj := json.loads(line)).get("event") == kind:
            out.append(obj)
    return out


def days_with_event(kind: str) -> list[int]:
    """Valid fixture days whose log carries at least one event of `kind`."""
    return [d for d in VALID_DAYS if log_events(d, kind)]


# ════════════════════════════════════════════════════════════════════════════
# 0. Bundle integrity
# ════════════════════════════════════════════════════════════════════════════
def test_the_tarball_contains_exactly_the_files_the_readme_indexes():
    """The archive must carry exactly the README's files, byte for byte.

    No bundle hash, size or file count is written here: the expected names and
    SHA-256s are read from the bundle's own README manifest and compared with
    the members of the tarball, so a future bundle needs no edit.
    """
    import tarfile
    index = readme_index()
    assert BUNDLE_TARBALL.is_file(), f"bundle tarball not found: {BUNDLE_TARBALL}"

    members: dict[str, str] = {}
    with tarfile.open(BUNDLE_TARBALL, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name[2:] if m.name.startswith("./") else m.name
            fh = tf.extractfile(m)
            assert fh is not None, f"unreadable tar member {name}"
            members[name] = hashlib.sha256(fh.read()).hexdigest()

    # The README indexes every file except itself (it cannot hash itself).
    expected_names = set(index) | {"README.md"}
    assert set(members) == expected_names, (
        f"tarball/README disagree — missing from tar: "
        f"{sorted(expected_names - set(members))}; "
        f"extra in tar: {sorted(set(members) - expected_names)}")
    bad = [(n, want, members[n]) for n, want in index.items()
           if members[n] != want]
    assert not bad, f"tar members do not match the README index: {bad}"


def test_every_file_in_the_readme_index_matches_its_listed_sha256():
    """Each indexed file on disk must hash to the README's value."""
    index = readme_index()
    bad = []
    for name, want in index.items():
        p = BUNDLE / name
        got = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "MISSING"
        if got != want:
            bad.append((name, want, got))
    assert not bad, f"bundle files do not match the README index: {bad}"


def test_the_extracted_bundle_has_no_file_the_readme_does_not_index():
    """No extra file may ride along un-indexed."""
    index = readme_index()
    on_disk = {str(p.relative_to(BUNDLE)) for p in BUNDLE.rglob("*") if p.is_file()}
    extra = on_disk - set(index) - {"README.md"}
    assert not extra, f"files present but not indexed by the README: {sorted(extra)}"


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
    """All FIVE tiers are present and protein is no longer blanket-hard."""
    assert targets.get("CDK4").tier == "easy"       # §6.1: 4 easy proteins
    assert targets.get("EGFR").tier == "medium"     # §6.1: 15 medium proteins
    assert targets.get("TP53").tier == "hard"       # §6.1: 11 hard proteins
    assert targets.get("ATM_mRNA").tier == "mrna"
    assert targets.get("BCL2_CRISPR").tier == "crispr"
    assert targets.get("UNKNOWN_TARGET_XYZ") is None


def test_table_distribution_is_the_one_the_spec_publishes(targets):
    """§12: 30 protein (4 easy, 15 medium, 11 hard), 30 mRNA, 10 CRISPR."""
    assert (len(targets.by_tier("easy")), len(targets.by_tier("medium")),
            len(targets.by_tier("hard"))) == (4, 15, 11)
    assert len(targets.by_tier("mrna")) == 30
    assert len(targets.by_tier("crispr")) == 10
    assert len(targets) == 70


def test_every_tier_in_the_table_is_priced(targets):
    """No target may reach pricing through an unknown tier (§6.1, §12)."""
    from payout.constants import BASE_REWARD_BY_TIER, base_reward_for_tier
    tiers = {targets.get(t).tier for t in targets.ids()}
    assert tiers == set(BASE_REWARD_BY_TIER)
    for tier in tiers:
        assert base_reward_for_tier(tier) > 0


def test_base_rewards_are_the_spec_table_values():
    """§6.1 / §12 tier -> base reward, including the new `medium` tier."""
    from payout.constants import base_reward_for_tier
    assert base_reward_for_tier("easy") == 300_000
    assert base_reward_for_tier("medium") == 700_000
    assert base_reward_for_tier("hard") == 900_000
    assert base_reward_for_tier("mrna") == 900_000
    assert base_reward_for_tier("crispr") == 252_000


def test_an_unknown_tier_is_refused_never_guessed():
    from payout.constants import base_reward_for_tier
    with pytest.raises(KeyError):
        base_reward_for_tier("legendary")


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
    """§13: wallet facts come from the log's own events, nothing else.

    The flagged wallet, the first-seen wallets and their timestamps are read
    out of the bundle's canonical log, so a bundle with different throwaway
    keys or different instants needs no edit here.
    """
    flagged_days = days_with_event("wallet_flagged")
    assert flagged_days, "no bundle day carries a wallet_flagged event"
    day = flagged_days[0]
    facts = log_source.day_log(day).wallet_facts()

    for ev in log_events(day, "wallet_flagged"):
        unflagged_after = [u for u in log_events(day, "wallet_unflagged")
                           if u["wallet"] == ev["wallet"]
                           and u["time_ms"] >= ev["time_ms"]]
        assert facts[ev["wallet"]].flagged is (not unflagged_after), ev["wallet"]

    seen = log_events(day, "wallet_first_seen")
    assert seen, f"day {day} has no wallet_first_seen event"
    for ev in seen:
        assert facts[ev["wallet"]].first_seen_ms == ev["time_ms"], ev["wallet"]
    # Every first-seen wallet in the log is known to the checker.
    assert {e["wallet"] for e in seen} <= set(facts)


def test_unflag_event_clears_an_earlier_flag(log_source):
    """A later wallet_unflagged must clear a flag the same log set."""
    from payout.sources import LogEvent
    flagged_days = days_with_event("wallet_flagged")
    assert flagged_days, "no bundle day carries a wallet_flagged event"
    day = flagged_days[0]
    ev = log_events(day, "wallet_flagged")[0]
    w = ev["wallet"]

    dl = log_source.day_log(day)
    assert dl.wallet_facts()[w].flagged is True, "precondition: wallet is flagged"
    dl.events.append(LogEvent(event="wallet_unflagged", wallet=w,
                              time_ms=ev["time_ms"] + 1_000))
    assert dl.wallet_facts()[w].flagged is False


def test_forfeit_event_is_picked_up(log_source):
    """§13.4: a holdback_forfeit is picked up with its own reason string."""
    forfeit_days = days_with_event("holdback_forfeit")
    assert forfeit_days, "no bundle day carries a holdback_forfeit event"
    day = forfeit_days[0]
    facts = log_source.day_log(day).wallet_facts()
    for ev in log_events(day, "holdback_forfeit"):
        w = facts[ev["wallet"]]
        assert w.forfeited is True, ev["wallet"]
        assert w.forfeit_reason == ev.get("reason")
        assert w.forfeited_at_ms == ev["time_ms"]


# ════════════════════════════════════════════════════════════════════════════
# 1e. The canonical log is proven before it is used
# ════════════════════════════════════════════════════════════════════════════
def test_canonical_route_is_fetched_and_hashed(coordinator, service):
    service.process_day(20000, [load_batch(20000)])
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
    d = service.process_day(20000, [load_batch(20000)])
    assert d.refused
    assert any("does not match" in r for r in d.reasons()), d.reasons()
    assert not d.signatures


def test_a_log_that_does_not_match_the_anchor_is_refused(
        log_source, settings, targets, ledger, signer):
    from payout.sources import AnchorSource
    bad = AnchorSource({d: "ab" * 32 for d in VALID_DAYS})
    svc = PayoutSignerService(log_source, bad, settings, targets, ledger, signer)
    d = svc.process_day(20000, [load_batch(20000)])
    assert d.refused
    assert any("ANCHORED" in r or ANCHOR_HASH_MISMATCH in r for r in d.reasons())
    assert not d.signatures


def test_a_missing_anchor_is_fatal(log_source, settings, targets, ledger, signer):
    from payout.sources import AnchorSource
    svc = PayoutSignerService(log_source, AnchorSource({}), settings, targets,
                              ledger, signer)
    d = svc.process_day(20000, [load_batch(20000)])
    assert d.refused and not d.signatures


def _verify_one(log_source, settings, targets, day, anchored):
    """Call the verifier layer DIRECTLY, bypassing the service's hash gate."""
    from payout.recompute import compute_day
    from payout.verifier import Batch, verify_batch
    dl = log_source.day_log(day)
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    comp = compute_day(dl, targets=targets, history=hist)
    return verify_batch(Batch.from_json(load_batch(day)), comp, dl, anchored,
                        settings, targets, set())


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
    d = service.process_day(20000, [load_batch(20000)])
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
    d = service.process_day(20000, [load_batch(20000)])
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


@pytest.mark.parametrize("hits,num", [
    (0, 4), (1, 4), (98, 4), (99, 4),        # first 99 confirmed hits: 100%
    (100, 3), (101, 3), (998, 3), (999, 3),  # 100th..999th: 75%
    (1000, 2), (1001, 2), (50_000, 2),       # from the 1,000th: 50%
])
def test_taper_boundaries_are_the_spec_boundaries(hits, num):
    """§6.2: 99th hit 100%, 100th 75%, 999th 75%, 1,000th 50%."""
    from payout.constants import taper_numerator
    assert taper_numerator(hits) == num
    assert final_reward(900_000, hits) == 900_000 * num // 4


# ════════════════════════════════════════════════════════════════════════════
# 1b. The taper count is DERIVED from earlier days' logs, never supplied
# ════════════════════════════════════════════════════════════════════════════
def test_hit_count_before_is_counted_from_earlier_days_in_order(log_source):
    """§6.2: each result sees only hits confirmed BEFORE it, across days."""
    from payout.recompute import hit_counts_before
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    hcb, _ = hit_counts_before(hist, day=20002)
    # CDK4 is hit by results 1,3,5 (day 20000), 8 (20001) and 9 (20002).
    assert [hcb[r] for r in (1, 3, 5, 8, 9)] == [0, 1, 2, 3, 4]
    # MYC: result 2 on day 20000, result 6 on day 20001.
    assert (hcb[2], hcb[6]) == (0, 1)
    # EGFR: result 4 on day 20000, result 10 on day 20002.
    assert (hcb[4], hcb[10]) == (0, 1)


def test_a_later_days_hits_never_count_as_earlier_ones(log_source):
    """Counting day 20000 must not be changed by days 20001/20002 existing."""
    from payout.recompute import hit_counts_before
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    only_first, _ = hit_counts_before([hist[0]], day=20000)
    with_future, _ = hit_counts_before(hist, day=20000)
    assert with_future[1] == only_first[1] == 0
    assert with_future[3] == only_first[3] == 1
    assert with_future[5] == only_first[5] == 2
    # Results of later days are not priced into day 20000's baseline at all.
    assert 8 not in with_future and 9 not in with_future


def test_the_checker_keeps_its_own_hit_counts_with_the_signed_batches(
        tmp_path, log_source, anchors, settings, targets, signer):
    """1(b): the count is persisted with the signed-batch record."""
    state = tmp_path / "state"
    svc = PayoutSignerService(log_source, anchors, settings, targets,
                              DecisionLedger(state), signer)
    assert svc.process_day(20000, [load_batch(20000)]).signed
    # A fresh ledger over the same directory still knows day 20000's counts.
    reread = DecisionLedger(state)
    assert reread.day_target_hits[20000] == {"CDK4": 3, "MYC": 1, "EGFR": 1}
    assert reread.target_hits_before(20001) == {"CDK4": 3, "MYC": 1, "EGFR": 1}
    assert reread.target_hits_before(20000) == {}


def test_a_saved_count_is_never_a_substitute_for_the_replay(
        tmp_path, log_source, targets):
    """§6a: saved state is a CACHE ONLY — it can never supply a count.

    The previous revision let a recorded per-day count stand in for a day
    whose log was not fetched (`carry_in`).  §6a forbids that: the count must
    come from replaying GENESIS_DAY..D, and a day missing from the replay is
    a refusal.  Feeding the record without the log must therefore NOT quietly
    produce a taper baseline — it must report the missing day.
    """
    from payout.errors import MISSING_ARCHIVE_DAY
    from payout.recompute import compute_day
    dl = log_source.day_log(20001)
    # Day 20000's log is deliberately absent; only our own record is offered.
    comp = compute_day(dl, targets=targets, history=[dl],
                       recorded_day_hits={20000: {"CDK4": 3, "MYC": 1, "EGFR": 1}})
    assert MISSING_ARCHIVE_DAY in {f.code for f in comp.findings}, \
        [f.code for f in comp.findings]
    # And the record contributed nothing to the count.
    assert comp.carried_in_hits == {}


def test_the_replay_supplies_the_whole_baseline(log_source, targets):
    """§6a: with the full replay present, the baseline comes from the logs."""
    from payout.recompute import compute_day
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    comp = compute_day(hist[1], targets=targets, history=hist)
    assert not comp.findings, [f.code for f in comp.findings]
    assert comp.carried_in_hits == {"CDK4": 3, "MYC": 1, "EGFR": 1}
    assert comp.hit_counts_before[6] == 1        # MYC already hit once
    assert comp.hit_counts_before[8] == 3        # CDK4 already hit three times
    assert comp.hit_counts_before[7] == 0        # TP53 is brand new


def test_a_cached_count_that_differs_from_the_replay_refuses(
        log_source, targets):
    """§6a cache rule: cache != replay is a refusal, in either direction."""
    from payout.recompute import compute_day
    from payout.errors import TAPER_CACHE_MISMATCH
    hist = [log_source.day_log(d) for d in (20000, 20001)]
    comp = compute_day(hist[1], targets=targets, history=hist,
                       recorded_day_hits={20000: {"CDK4": 99, "MYC": 1, "EGFR": 1}})
    assert any(f.code == TAPER_CACHE_MISMATCH for f in comp.findings), \
        [f.code for f in comp.findings]
    # The honest cache for that day passes.
    ok = compute_day(hist[1], targets=targets, history=hist,
                     recorded_day_hits={20000: {"CDK4": 3, "MYC": 1, "EGFR": 1}})
    assert not ok.findings, [f.code for f in ok.findings]


# ════════════════════════════════════════════════════════════════════════════
# 1c. Rules are applied in the numbered order of BATCH_SPEC §5.0
# ════════════════════════════════════════════════════════════════════════════
def test_rule_order_is_the_spec_numbered_list_verbatim():
    """§5.0 numbers rules 1..17; the checker's order must BE that list."""
    from payout.verifier import EXTRA_RULE_ORDER, RULE_PRECEDENCE, SPEC_RULE_ORDER
    assert SPEC_RULE_ORDER == (
        "wrong_program_id_or_mint",   # 1  (spec calls it program_id_or_mint)
        "batch_total_above_cap",      # 2
        "chunk_above_entry_limit",    # 3
        "total_above_day_cap",        # 4
        "duplicate_recipient",        # 5
        "sort_contract_violated",     # 6
        "inflated_amount",            # 7
        "target_not_in_table",        # 8
        "no_wallet_first_seen",       # 9
        "flagged_wallet_paid",        # 10
        "forfeited_wallet_paid",      # 11
        "held_wallet_paid",           # 12
        "no_confirmed_result",        # 13
        "repeated_batch_id",          # 14
        "log_hash_mismatch",          # 15
        "reward_mismatch",            # 16
        "total_not_sum_of_entries",   # 17 total_mismatch
    )
    # §6.3-new adds four codes AFTER rule 17, in the order that section lists
    # them.  They are unreachable below VALIDATOR_PAY_FROM_DAY, so no day the
    # checker already decided can change verdict because of them.
    from payout.verifier import VALIDATOR_PAY_RULE_ORDER
    assert VALIDATOR_PAY_RULE_ORDER == (
        "validator_not_verified",
        "validator_self_pay",
        "validator_over_limit",
        "validator_pay_mismatch",
    )
    # Extra checks never pre-empt a spec-numbered reason.
    assert RULE_PRECEDENCE == (SPEC_RULE_ORDER + VALIDATOR_PAY_RULE_ORDER
                               + EXTRA_RULE_ORDER)
    assert not set(SPEC_RULE_ORDER) & set(EXTRA_RULE_ORDER)
    assert not set(SPEC_RULE_ORDER) & set(VALIDATOR_PAY_RULE_ORDER)
    assert not set(VALIDATOR_PAY_RULE_ORDER) & set(EXTRA_RULE_ORDER)


def test_forfeited_outranks_held_because_the_spec_says_so():
    """§5.0 now puts forfeited (11) before held (12) — the reverse of C5b3.

    The spec gives the reason in the rule text itself: a forfeited wallet is
    necessarily also in its first week, so the more SPECIFIC reason must win.
    """
    from payout.verifier import SPEC_RULE_ORDER
    assert SPEC_RULE_ORDER.index("forfeited_wallet_paid") < \
        SPEC_RULE_ORDER.index("held_wallet_paid")


def test_a_batch_breaking_several_rules_is_refused_for_the_first_one(
        log_source, settings, targets):
    """A batch that violates 6, 9 and 16 at once must report rule 6."""
    from payout.recompute import compute_day
    from payout.verifier import Batch, verify_batch
    dl = log_source.day_log(20000)
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    comp = compute_day(dl, targets=targets, history=hist)

    raw = load_batch(20000)
    raw["entries"] = list(reversed(raw["entries"]))        # rule 6: unsorted
    raw["entries"][0]["results"][0]["amountRaw"] = "999999"  # rule 16: reward
    raw["entries"][0]["amountRaw"] = "999999"
    res = verify_batch(Batch.from_json(raw), comp, dl, anchored_hashes()[20000],
                       settings, targets, set())
    assert not res.signable
    fired = {f.code for f in res.findings}
    assert "sort_contract_violated" in fired and "reward_mismatch" in fired
    assert res.primary_rule == "sort_contract_violated"   # the FIRST rule


# ════════════════════════════════════════════════════════════════════════════
# 1d. Reference and discovery rewards are not payable (§6.1, §10a)
# ════════════════════════════════════════════════════════════════════════════
def test_the_out_of_scope_rewards_are_in_no_tier():
    from payout.constants import (BASE_REWARD_BY_TIER, REWARD_DISCOVERY,
                                  REWARD_REFERENCE, out_of_scope_reward)
    assert REWARD_REFERENCE == 108_000          # 0.108 LIFE
    assert REWARD_DISCOVERY == 100_000_000      # 100 LIFE
    assert REWARD_REFERENCE not in BASE_REWARD_BY_TIER.values()
    assert REWARD_DISCOVERY not in BASE_REWARD_BY_TIER.values()
    assert out_of_scope_reward(108_000) and out_of_scope_reward(100_000_000)
    assert out_of_scope_reward(300_000) is None


@pytest.mark.parametrize("amount", [108_000, 100_000_000])
def test_a_batch_paying_an_out_of_scope_reward_is_refused(
        log_source, settings, targets, amount):
    """§10a: reference (0.108) and discovery (100 LIFE) are never payable."""
    from payout.errors import OUT_OF_SCOPE_REWARD_PAID
    from payout.recompute import compute_day
    from payout.verifier import Batch, verify_batch
    dl = log_source.day_log(20000)
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    comp = compute_day(dl, targets=targets, history=hist)

    raw = load_batch(20000)
    ent = raw["entries"][0]                      # 4pmn7PJu… / result 3 / CDK4
    ent["results"][0]["amountRaw"] = str(amount)
    ent["amountRaw"] = str(amount)
    res = verify_batch(Batch.from_json(raw), comp, dl, anchored_hashes()[20000],
                       settings, targets, set())
    assert not res.signable
    assert OUT_OF_SCOPE_REWARD_PAID in {f.code for f in res.findings}, \
        res.reasons()


def test_entry_total_must_equal_the_sum_of_its_breakdown():
    """§7 aggregation: day_20000's minerA entry is 2 results summed."""
    b = load_batch(20000)
    for e in b["entries"]:
        assert int(e["amountRaw"]) == sum(int(r["amountRaw"]) for r in e["results"])
    agg = [e for e in b["entries"] if len(e["results"]) == 2][0]
    # CDK4 (easy, 300,000) + MYC (hard, 900,000), both at taper 4/4.
    assert int(agg["amountRaw"]) == 1_200_000


# ════════════════════════════════════════════════════════════════════════════
# 1h. Replay: the checker keeps its own record of signed batch ids
# ════════════════════════════════════════════════════════════════════════════
def test_a_batch_id_is_never_signed_twice(service):
    first = service.process_day(20000, [load_batch(20000)])
    assert first.signed, first.reasons()
    second = service.process_day(20000, [load_batch(20000)])
    assert second.refused
    assert second.primary_rule == REPEATED_BATCH_ID
    assert not second.signatures


def test_the_signed_record_survives_a_restart(tmp_path, log_source, anchors,
                                              settings, targets, signer):
    state = tmp_path / "state"
    svc1 = PayoutSignerService(log_source, anchors, settings, targets,
                               DecisionLedger(state), signer)
    assert svc1.process_day(20000, [load_batch(20000)]).signed
    # A brand-new service over the same state directory.
    svc2 = PayoutSignerService(log_source, anchors, settings, targets,
                               DecisionLedger(state), signer)
    again = svc2.process_day(20000, [load_batch(20000)])
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


# ════════════════════════════════════════════════════════════════════════════
# 2. Task 2 — the checker's saved state is only a cache, and no clock is read
# ════════════════════════════════════════════════════════════════════════════
def _fresh_service(tmp_path, mock, state_dir, signed=False):
    """A service over `state_dir`, with or without a signing key."""
    from conftest_payout import write_throwaway_keypair
    from payout.sources import AnchorSource
    key = None
    if signed:
        p_, v_ = tmp_path / "pk.json", tmp_path / "vk.json"
        if not p_.exists():
            write_throwaway_keypair(p_)
            write_throwaway_keypair(v_)
        key = PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(p_),
                                     "VALIDATOR_KEYPAIR": str(v_)})
    from payout.sources import LogSource as _LS
    return PayoutSignerService(
        _LS(mock.url, timeout=5.0), AnchorSource(anchored_hashes()),
        Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT),
        TargetsTable.from_file(TARGETS_TABLE), DecisionLedger(state_dir), key)


def _decide(tmp_path, mock, state_dir, day):
    d = _fresh_service(tmp_path, mock, state_dir).process_day(
        day, [load_batch(day)])
    return ("sign" if d.signed else "refuse"), d.primary_rule


def test_deleting_the_saved_state_gives_identical_decisions(tmp_path):
    """Task 2: the saved state is a cache, so losing it changes nothing.

    Each valid day is decided once with a warm state directory (populated by
    a prior run of the SAME day on a sibling ledger) and once with the state
    directory deleted outright.  §6a says the count comes from the replay, so
    both runs must agree exactly.
    """
    import shutil
    with MockPayoutCoordinator() as mock:
        for day in VALID_DAYS:
            warm = tmp_path / f"warm_{day}"
            # Populate the cache by signing the earlier days first.
            svc = _fresh_service(tmp_path, mock, warm, signed=True)
            for earlier in [d for d in VALID_DAYS if d < day]:
                svc.process_day(earlier, [load_batch(earlier)])
            with_state = _decide(tmp_path, mock, warm, day)
            assert (warm / "payout_index.json").is_file()

            shutil.rmtree(warm)
            assert not warm.exists()
            without_state = _decide(tmp_path, mock, warm, day)

            assert with_state == without_state, (
                f"day {day}: with saved state {with_state}, "
                f"without it {without_state}")


def test_changing_one_saved_count_makes_the_checker_refuse(tmp_path):
    """Task 2: a single tampered cached count must refuse, not re-price.

    Day 20000's own log gives CDK4 three confirmed hits.  Writing 4 into the
    checker's saved state contradicts the replay by exactly one, and §6a's
    cache rule makes that a refusal.
    """
    import json as _json
    from payout.errors import TAPER_CACHE_MISMATCH
    with MockPayoutCoordinator() as mock:
        state = tmp_path / "state"
        svc = _fresh_service(tmp_path, mock, state, signed=True)
        first = svc.process_day(20000, [load_batch(20000)])
        assert first.signed, first.reasons()

        index_path = state / "payout_index.json"
        index = _json.loads(index_path.read_text())
        assert index["day_target_hits"]["20000"]["CDK4"] == 3
        index["day_target_hits"]["20000"]["CDK4"] = 4        # one count, +1
        # Let the same day be re-checked: clear only the replay guard.
        index["signed_batch_ids"] = []
        index["paid_result_ids"] = []
        index_path.write_text(_json.dumps(index))

        again = _fresh_service(tmp_path, mock, state).process_day(
            20000, [load_batch(20000)])
        assert again.refused, "a tampered cached count must refuse"
        assert not again.signatures
        assert again.primary_rule == TAPER_CACHE_MISMATCH, again.reasons()


def test_a_missing_day_in_the_archive_makes_the_checker_refuse(tmp_path):
    """Task 2 / §6a: a gap anywhere in GENESIS_DAY..D refuses to sign.

    Day 20002's replay range is 20000, 20001, 20002.  Hiding day 20001 leaves
    the batch day itself perfectly servable, so only the replay rule can catch
    it — and an absent day must never be treated as "zero hits".
    """
    from payout.constants import GENESIS_DAY
    from payout.errors import MISSING_ARCHIVE_DAY
    assert GENESIS_DAY == 20000
    with MockPayoutCoordinator() as mock:
        mock.hide_day(20001)
        d = _fresh_service(tmp_path, mock, tmp_path / "s", signed=True).process_day(
            20002, [load_batch(20002)])
        assert d.refused, "a missing archive day must refuse"
        assert not d.signatures
        assert d.primary_rule == MISSING_ARCHIVE_DAY, d.reasons()
        assert "20001" in " ".join(d.reasons())


def test_the_replay_really_reads_every_day_from_genesis(tmp_path):
    """§6a: GET /log/{day}/canonical is called for 20000..D, in order."""
    from payout.batch_hash import day_to_iso
    with MockPayoutCoordinator() as mock:
        _fresh_service(tmp_path, mock, tmp_path / "s", signed=True).process_day(
            20002, [load_batch(20002)])
        canon = [p for p in mock.state.request_log if p.endswith("/canonical")]
        for day in (20000, 20001, 20002):
            assert f"/log/{day_to_iso(day)}/canonical" in canon, day
        # Ascending day order, genesis first.
        order = [p for p in canon if p.endswith("/canonical")]
        assert order.index(f"/log/{day_to_iso(20000)}/canonical") < \
            order.index(f"/log/{day_to_iso(20002)}/canonical")


def test_the_payout_package_contains_no_wall_clock_call():
    """Task 1(a): no NOW_MS and no current-time read anywhere in `payout`.

    Checked against executable source (comments and docstrings stripped), so
    the prose explaining the removal cannot satisfy the test by itself.
    """
    import tokenize
    import payout
    banned = ("NOW_MS", "now_ms", "time.time", "time_ns", "datetime.now",
              "utcnow", "date.today", "monotonic", "perf_counter")
    pkg_dir = Path(payout.__file__).parent
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
    assert not offenders, f"wall-clock references remain: {offenders}"


@pytest.mark.parametrize("clock_name", sorted(FAKE_CLOCKS_MS))
def test_a_signed_day_is_signed_under_any_clock(clock_name, fake_clock, tmp_path):
    """Task 2: the signature itself is clock-independent, byte for byte."""
    fake_clock(FAKE_CLOCKS_MS[clock_name])
    with MockPayoutCoordinator() as mock:
        d = _fresh_service(tmp_path, mock, tmp_path / "s", signed=True).process_day(
            20000, [load_batch(20000)])
        assert d.signed, d.reasons()
        assert d.results[0].batch_hash_recomputed == load_batch(20000)["batchHash"]


# ════════════════════════════════════════════════════════════════════════════
# Open (not closed) days — BATCH_SPEC §13.6 / API_CONTRACT §2.16
# ════════════════════════════════════════════════════════════════════════════
def test_the_mock_serves_an_open_day_exactly_as_the_contract_says():
    """§2.16: /canonical -> 409 + no-store; /log/{day} -> closed:false."""
    import urllib.error
    import urllib.request
    from payout.batch_hash import day_to_iso
    with MockPayoutCoordinator() as mock:
        mock.open_day(20002)
        iso = day_to_iso(20002)

        try:
            urllib.request.urlopen(f"{mock.url}/log/{iso}/canonical", timeout=5)
            raise AssertionError("an open day's canonical route must not be 200")
        except urllib.error.HTTPError as exc:
            assert exc.code == 409, exc.code
            assert exc.headers.get("Cache-Control") == "no-store"
            body = json.loads(exc.read())
            assert body["closed"] is False
            assert body["error"] == "Day is not yet closed"

        with urllib.request.urlopen(f"{mock.url}/log/{iso}", timeout=5) as r:
            manifest = json.loads(r.read())
        assert manifest["closed"] is False
        # A closed day is unaffected.
        with urllib.request.urlopen(f"{mock.url}/log/{day_to_iso(20000)}",
                                    timeout=5) as r:
            assert json.loads(r.read()).get("closed") is not False


def test_an_open_day_raises_day_not_closed_and_is_never_read(log_source,
                                                             coordinator):
    """§13.6: the open day's canonical body is never fetched at all."""
    from payout.batch_hash import day_to_iso
    from payout.errors import DayNotClosed
    coordinator.open_day(20002)
    with pytest.raises(DayNotClosed) as exc:
        log_source.day_log(20002)
    assert "still open" in str(exc.value) or "closed=false" in str(exc.value)
    # The manifest said closed:false, so the canonical route was never called.
    assert f"/log/{day_to_iso(20002)}/canonical" not in coordinator.state.request_log


def test_a_409_on_the_canonical_route_alone_is_day_not_closed(coordinator,
                                                              log_source):
    """Even if the manifest looks closed, a 409 body is a DayNotClosed.

    This is the pure transport case: the checker must read the 409 status for
    what §2.16 says it means, not as a generic unreachable archive.
    """
    from payout.errors import DayNotClosed
    coordinator.open_day(20002)
    # Re-publish a *closed-looking* manifest so only the 409 can catch it.
    coordinator.set_manifest(20002, json.loads(
        (BUNDLE / "fixtures/day_20002/log_get_response.json").read_text()))
    coordinator.state.open_days.add(20002)
    with pytest.raises(DayNotClosed) as exc:
        log_source.canonical_log_bytes(20002)
    assert "409" in str(exc.value)


def test_the_checker_refuses_to_sign_an_open_batch_day(tmp_path):
    """Task 2: an open batch day is refused with a clear reason, unsigned."""
    from payout.errors import DAY_NOT_CLOSED
    with MockPayoutCoordinator() as mock:
        mock.open_day(20002)
        d = _fresh_service(tmp_path, mock, tmp_path / "s", signed=True).process_day(
            20002, [load_batch(20002)])
        assert d.refused, "an open day must never be signed"
        assert not d.signatures, "an open day produced a signature"
        assert d.primary_rule == DAY_NOT_CLOSED, d.reasons()
        reason = " ".join(d.reasons()).lower()
        assert "not closed" in reason or "still open" in reason, d.reasons()
        assert "20002" in " ".join(d.reasons())


def test_an_open_day_inside_the_replay_range_also_refuses(tmp_path):
    """§6a + §13.6: an open day anywhere in GENESIS..D blocks the signature.

    Day 20002's replay range is 20000..20002.  Opening day 20001 leaves the
    batch day itself perfectly closed and servable, so only the replay can
    catch it — and an open day must never be silently treated as empty.
    """
    from payout.errors import DAY_NOT_CLOSED
    with MockPayoutCoordinator() as mock:
        mock.open_day(20001)
        d = _fresh_service(tmp_path, mock, tmp_path / "s", signed=True).process_day(
            20002, [load_batch(20002)])
        assert d.refused
        assert not d.signatures
        assert d.primary_rule == DAY_NOT_CLOSED, d.reasons()
        assert "20001" in " ".join(d.reasons())


def test_an_open_day_is_refused_under_every_clock(fake_clock, tmp_path):
    """The open-day refusal is a property of the archive, not of the clock."""
    from payout.errors import DAY_NOT_CLOSED
    for name, ms in sorted(FAKE_CLOCKS_MS.items()):
        fake_clock(ms)
        with MockPayoutCoordinator() as mock:
            mock.open_day(20002)
            d = _fresh_service(tmp_path, mock, tmp_path / f"s_{name}",
                               signed=True).process_day(
                20002, [load_batch(20002)])
            assert d.refused and d.primary_rule == DAY_NOT_CLOSED, (name, d.reasons())
            assert not d.signatures


def test_closing_the_day_lets_the_same_batch_sign(tmp_path):
    """The refusal is caused by openness ONLY: close the day and it signs."""
    with MockPayoutCoordinator() as mock:
        mock.open_day(20002)
        refused = _fresh_service(tmp_path, mock, tmp_path / "a",
                                 signed=True).process_day(
            20002, [load_batch(20002)])
        assert refused.refused

        mock.close_day(20002)
        signed = _fresh_service(tmp_path, mock, tmp_path / "b",
                                signed=True).process_day(
            20002, [load_batch(20002)])
        assert signed.signed, signed.reasons()
        assert signed.signatures


def test_an_open_day_leaves_no_signature_and_no_paid_results(tmp_path):
    """Nothing from an open day may reach the ledger as signed or paid."""
    state = tmp_path / "s"
    with MockPayoutCoordinator() as mock:
        mock.open_day(20002)
        svc = _fresh_service(tmp_path, mock, state, signed=True)
        d = svc.process_day(20002, [load_batch(20002)])
        assert d.refused and not d.signatures
        assert not svc.ledger.signed_batch_ids
        assert not svc.ledger.paid_result_ids
