"""Step 3 — run EVERY bundle fixture through the checker and compare.

This module is the acceptance gate: the three valid days must be SIGNED and
every tampered variant must be REFUSED for exactly the rule named in its
`.verdict.json`.  The run fails on ANY difference.

There are no expected failures, no xfails and no defect allowances: with the
current bundle every one of the 19 fixtures matches its published verdict, so
nothing needs tolerating.  If a fixture ever stops matching, this run fails —
which is the point.

C5b4: the checker takes no clock.  `process_day` receives the batch's day
number and nothing else, and it derives the §6a replay range from
GENESIS_DAY itself, so neither this module nor any caller can narrow it.

Running this file directly prints the comparison table:

    ./.venv-test/bin/python tests/test_fixture_table.py
"""
from __future__ import annotations

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
    ledger,
    log_source,
    payout_key_paths,
    settings,
    signer,
    service,
    targets,
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

from payout.constants import GENESIS_DAY  # noqa: E402
from payout.ledger import DecisionLedger  # noqa: E402
from payout.service import PayoutSignerService  # noqa: E402
from payout.signer_key import PayoutSigner  # noqa: E402
from payout.sources import AnchorSource, LogSource, Settings, TargetsTable  # noqa: E402

# Variants whose batch targets a day other than 20000.
VARIANT_DAY = {"06_held_wallet_paid": 20002, "07_forfeited_wallet_paid": 20001}

# 11_repeated_batch_id only means anything if that id was already signed, so
# the checker's own record is pre-seeded with it — exercising the replay
# guard rather than re-deriving it from a chain.
PRESEED_SIGNED = {"11_repeated_batch_id": [20000000]}


def _run_one(batch: dict, day: int, *, preseed_signed=(), state_dir: Path):
    """Verify a single batch against the fixtures; return the DayDecision."""
    with MockPayoutCoordinator() as mock:
        ledger = DecisionLedger(state_dir)
        for bid in preseed_signed:
            ledger.mark_signed(bid, [])
        svc = PayoutSignerService(
            LogSource(mock.url, timeout=5.0),
            AnchorSource(anchored_hashes()),
            Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT),
            TargetsTable.from_file(TARGETS_TABLE),
            ledger,
            signer=None,            # verification only; no key needed to refuse
        )
        return svc.process_day(day, [batch])


def _valid_day_outcome(day: int, state_dir: Path) -> tuple[str, str | None]:
    with MockPayoutCoordinator() as mock:
        kp_dir = state_dir / "keys"
        kp_dir.mkdir(parents=True, exist_ok=True)
        from conftest_payout import write_throwaway_keypair
        p, v = kp_dir / "payout.json", kp_dir / "validator.json"
        write_throwaway_keypair(p)
        write_throwaway_keypair(v)
        svc = PayoutSignerService(
            LogSource(mock.url, timeout=5.0),
            AnchorSource(anchored_hashes()),
            Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT),
            TargetsTable.from_file(TARGETS_TABLE),
            DecisionLedger(state_dir),
            PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(p),
                                   "VALIDATOR_KEYPAIR": str(v)}),
        )
        d = svc.process_day(day, [load_batch(day)])
        return ("sign" if d.signed else "refuse"), d.primary_rule


# ── valid days ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("day", VALID_DAYS)
def test_valid_day(day, tmp_path):
    """Every valid day must be signed — no allowance, no xfail."""
    expected = load_expected_verdict(day)["verdict"]
    actual, rule = _valid_day_outcome(day, tmp_path)
    assert actual == expected, \
        f"day_{day}: expected {expected}, got {actual} ({rule})"


def test_signed_days_produce_a_signature_over_the_recomputed_hash(tmp_path):
    """A signed day really is signed, and over the §4 hash we recomputed."""
    with MockPayoutCoordinator() as mock:
        from conftest_payout import write_throwaway_keypair
        p, v = tmp_path / "p.json", tmp_path / "v.json"
        write_throwaway_keypair(p)
        write_throwaway_keypair(v)
        svc = PayoutSignerService(
            LogSource(mock.url, timeout=5.0), AnchorSource(anchored_hashes()),
            Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT),
            TargetsTable.from_file(TARGETS_TABLE), DecisionLedger(tmp_path / "s"),
            PayoutSigner.from_env({"PAYOUT_KEYPAIR": str(p),
                                   "VALIDATOR_KEYPAIR": str(v)}))
        d = svc.process_day(20000, [load_batch(20000)])
        assert d.signed, d.reasons()
        assert d.signatures[20000000]
        assert d.results[0].batch_hash_recomputed == load_batch(20000)["batchHash"]


# ── tampered variants ───────────────────────────────────────────────────────
@pytest.mark.parametrize("name", tampered_names())
def test_tampered_variant(name, tmp_path):
    """Every tampered variant is refused for EXACTLY its published rule."""
    expected = load_tampered_verdict(name)
    batch = load_tampered(name)
    day = VARIANT_DAY.get(name, 20000)
    decision = _run_one(batch, day, preseed_signed=PRESEED_SIGNED.get(name, ()),
                        state_dir=tmp_path)

    assert decision.refused, (
        f"{name} must be REFUSED but the checker was willing to sign it")
    assert not decision.signatures, f"{name} produced a signature"
    assert decision.primary_rule == expected["rule"], (
        f"{name}: expected rule {expected['rule']!r}, "
        f"got {decision.primary_rule!r}\n" + "\n".join(decision.reasons()))


def test_every_bundle_fixture_is_covered():
    """All 16 tampered variants plus 3 valid days are exercised."""
    assert len(tampered_names()) == 16
    assert len(VALID_DAYS) == 3


def test_no_defect_allowance_mechanism_remains():
    """Task 1(e): the defect allowance and its guard test are gone for good.

    Checked against the executable source of both test modules (comments and
    docstrings stripped, so the prose above may still name the old symbol)
    and against neither module importing or defining it.
    """
    import test_payout_signer
    assert not hasattr(test_payout_signer, "KNOWN_BUNDLE_DEFECTS")

    banned = ("KNOWN_BUNDLE_DEFECTS", "xfail", "KNOWN_BUNDLE_DEFECT")
    for mod_path in (Path(__file__), Path(test_payout_signer.__file__)):
        code = _executable_source(mod_path)
        for token in banned:
            assert token not in code, f"{mod_path.name} still references {token}"


def _executable_source(path: Path) -> str:
    """Module source with comments and string literals removed."""
    import tokenize
    out: list[str] = []
    with path.open("rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    return " ".join(out)


def test_the_checker_needs_no_feasible_clock_window():
    """C5b4: no instant has to be chosen for 06 and 07 to reach their rules.

    The previous revision pinned NOW_MS into a single 3-day window because
    §5.0 then ranked held (11) above forfeited (12).  §5.0 now ranks
    forfeited first AND states the holdback rule in day numbers, so both
    fixtures are decided by the batch day alone:

      07  wallet first seen 1727740800000 -> firstSeenDay 19997, forfeited.
          Batch day 20001 < 19997+7 = 20004, so it IS also held — but
          forfeiture is rule 11 and held is rule 12, so the more specific
          `forfeited_wallet_paid` wins, exactly as §5.0 says it should.
      06  wallet first seen 1728000000000 -> firstSeenDay 20000, not forfeited.
          Batch day 20002 < 20000+7 = 20007, so `held_wallet_paid` fires.

    The three valid days are untouched: their wallets are first seen on day
    19986 and released on 19993, well before batch days 20000..20002.
    """
    from payout.constants import HOLDBACK_DAYS, day_of_ms, is_held_on_day
    from payout.verifier import SPEC_RULE_ORDER

    assert day_of_ms(1_727_740_800_000) == 19997        # fixture 07's wallet
    assert is_held_on_day(1_727_740_800_000, 20001)     # held AND forfeited
    assert SPEC_RULE_ORDER.index("forfeited_wallet_paid") < \
        SPEC_RULE_ORDER.index("held_wallet_paid")       # forfeiture wins

    assert day_of_ms(1_728_000_000_000) == 20000        # fixture 06's wallet
    assert is_held_on_day(1_728_000_000_000, 20002)     # 20002 < 20000 + 7
    assert HOLDBACK_DAYS == 7

    # The valid days' wallets are long released, so nothing is held there.
    for batch_day in VALID_DAYS:
        assert not is_held_on_day(1_726_790_400_000, batch_day)

    # And the symbols that encoded the old hand-picked clock are gone.
    import conftest_payout
    assert not hasattr(conftest_payout, "NOW_MS")
    assert not hasattr(conftest_payout, "HOLDBACK_WINDOW_MIN_MS")
    assert not hasattr(conftest_payout, "HOLDBACK_WINDOW_MAX_MS")


# ── the printable table (step 3) ────────────────────────────────────────────
def build_table(tmp_root: Path) -> tuple[list[tuple], int]:
    rows: list[tuple] = []
    diffs = 0
    for day in VALID_DAYS:
        name = f"day_{day}"
        exp = load_expected_verdict(day)["verdict"]
        act, rule = _valid_day_outcome(day, tmp_root / name)
        ok = act == exp
        if not ok:
            diffs += 1
        rows.append((name, exp, "—", act, rule or "—", "OK" if ok else "DIFFERENCE"))

    for name in tampered_names():
        exp = load_tampered_verdict(name)
        batch = load_tampered(name)
        day = VARIANT_DAY.get(name, 20000)
        d = _run_one(batch, day, preseed_signed=PRESEED_SIGNED.get(name, ()),
                     state_dir=tmp_root / name)
        act = "refuse" if d.refused else "sign"
        rule = d.primary_rule or "—"
        ok = d.refused and rule == exp["rule"]
        if not ok:
            diffs += 1
        rows.append((name, exp["verdict"], exp["rule"], act, rule,
                     "OK" if ok else "DIFFERENCE"))
    return rows, diffs


def test_print_fixture_table(tmp_path, capsys):
    rows, diffs = build_table(tmp_path)
    with capsys.disabled():
        print()
        print(_render(rows))
    assert diffs == 0, f"{diffs} fixture(s) differ from their verdict file"


# ── task 2: the same answers under three different system clocks ───────────
@pytest.mark.parametrize("clock_name", sorted(FAKE_CLOCKS_MS))
def test_the_whole_fixture_set_is_identical_under_any_system_clock(
        clock_name, fake_clock, tmp_path):
    """Task 2: 2024, 2026 and 2030 must all produce the SAME 19 decisions.

    The clock is moved years away from the fixture dates in both directions.
    Any surviving wall-clock read would change a holdback decision and show
    up here as a different verdict or a different rule.
    """
    fake_clock(FAKE_CLOCKS_MS[clock_name])
    rows, diffs = build_table(tmp_path)
    assert diffs == 0, f"clock {clock_name}: {diffs} fixture(s) differ"
    # The decision tuple, independent of the clock that produced it.
    got = [(name, actual, actual_rule) for name, _e, _er, actual, actual_rule, _s in rows]
    assert len(got) == 19
    _record_clock_outcome(clock_name, got)


_CLOCK_OUTCOMES: dict[str, list[tuple]] = {}


def _record_clock_outcome(clock_name: str, rows: list[tuple]) -> None:
    _CLOCK_OUTCOMES[clock_name] = rows
    if len(_CLOCK_OUTCOMES) > 1:
        ref_name, ref = next(iter(sorted(_CLOCK_OUTCOMES.items())))
        for other, rows2 in sorted(_CLOCK_OUTCOMES.items()):
            assert rows2 == ref, (
                f"clock {other} disagrees with clock {ref_name}: "
                f"{[a for a, b in zip(rows2, ref) if a != b][:3]}")


def _render(rows) -> str:
    head = ("FIXTURE", "EXPECTED", "EXPECTED RULE", "ACTUAL", "ACTUAL RULE", "STATUS")
    data = [head] + [tuple(str(c) for c in r) for r in rows]
    w = [max(len(r[i]) for r in data) for i in range(len(head))]
    line = "  ".join("-" * x for x in w)
    out = ["  ".join(h.ljust(w[i]) for i, h in enumerate(head)), line]
    out += ["  ".join(r[i].ljust(w[i]) for i in range(len(head))) for r in data[1:]]
    out.append(line)
    ok = sum(1 for r in rows if r[5] == "OK")
    df = sum(1 for r in rows if r[5] == "DIFFERENCE")
    out.append(f"{len(rows)} fixtures: {ok} OK, {df} difference(s), "
               f"0 expected failure(s)")
    return "\n".join(out)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rows, diffs = build_table(Path(td))
    print(_render(rows))
    sys.exit(1 if diffs else 0)
