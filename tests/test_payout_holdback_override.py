"""HOLDBACK_DAYS_OVERRIDE — the DEVNET-ONLY holdback escape hatch.

The protocol constant is HOLDBACK_DAYS = 7 (BATCH_SPEC §5.1.3, §5.0 rule 12)
and the checker deliberately ignores a plain HOLDBACK_DAYS environment
variable.  The devnet coordinator, however, is deployed with a holdback of 0
so a test day can be paid as soon as it closes.

HOLDBACK_DAYS_OVERRIDE bridges that, under rules this module pins down:

  * honoured ONLY when PROGRAM_ID is one of the program ids hard-coded in
    `payout.constants.DEVNET_PROGRAM_IDS` — an allow-list no setting can
    extend;
  * for any other program id it is IGNORED, loudly, and 7 is still enforced
    (including an id that differs from a devnet id by ONE character);
  * a value that is negative or not an integer refuses start-up, on every
    network, before the program id is even consulted;
  * unset leaves the protocol constant 7;
  * the effect is CONSISTENT across both rules it touches — the held-wallet
    rule (12) and the first-week forfeiture rule (11): with 0 there is no
    first week, so neither can fire.

No chain, no coordinator, no key, no PM2 process is touched here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
for _p in (str(_TESTS), str(_TESTS.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from conftest_payout import (  # noqa: E402,F401
    LIFE_MINT,
    PROGRAM_ID,
    coordinator,
    log_source,
)
from mock_payout_coordinator import (                        # noqa: E402
    TARGETS_TABLE,
    VALID_DAYS,
    anchored_hashes,
    load_tampered,
)
from payout import constants as C                            # noqa: E402
from payout.errors import (                                  # noqa: E402
    FORFEITED_WALLET_PAID,
    HELD_WALLET_PAID,
    PayoutConfigError,
)
from payout.sources import Settings, TargetsTable            # noqa: E402

DEVNET_A = "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"
DEVNET_B = "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf"

# DEVNET_A with ONE character changed (leading H -> 2); still valid base58,
# still decodes to 32 bytes, and emphatically NOT the devnet program.
NEAR_MISS = "2hS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"


def _env(program_id: str, override=None) -> dict[str, str]:
    env = {"PROGRAM_ID": program_id, "LIFE_MINT": LIFE_MINT}
    if override is not None:
        env[C.HOLDBACK_OVERRIDE_ENV] = override
    return env


class _Warns:
    """Collects the loud 'override ignored' lines the CLI would print."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)


# ════════════════════════════════════════════════════════════════════════════
# The allow-list itself
# ════════════════════════════════════════════════════════════════════════════
def test_the_devnet_allow_list_is_exactly_the_two_hard_coded_ids():
    assert C.DEVNET_PROGRAM_IDS == (DEVNET_A, DEVNET_B)
    assert C.is_devnet_program_id(DEVNET_A)
    assert C.is_devnet_program_id(DEVNET_B)
    assert not C.is_devnet_program_id(NEAR_MISS)
    assert not C.is_devnet_program_id(PROGRAM_ID)


def test_the_protocol_constant_is_still_seven():
    assert C.HOLDBACK_DAYS == 7


# ════════════════════════════════════════════════════════════════════════════
# Honoured on each devnet program id
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("devnet_id", [DEVNET_A, DEVNET_B])
@pytest.mark.parametrize("value,expected", [("0", 0), ("3", 3), ("14", 14)])
def test_override_is_honoured_for_each_devnet_program_id(
        devnet_id, value, expected):
    warns = _Warns()
    s = Settings.from_env(_env(devnet_id, value), warn=warns)
    assert s.holdback_days == expected
    assert s.program_id == devnet_id
    # An honoured override is never reported as "ignored".
    assert warns.lines == []
    assert s.holdback_override_active is (expected != C.HOLDBACK_DAYS)


# ════════════════════════════════════════════════════════════════════════════
# Ignored — loudly — on anything else
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("other_id", [
    PROGRAM_ID,            # the mainnet-shaped fixture program
    NEAR_MISS,             # differs from DEVNET_A by exactly one character
])
def test_override_is_ignored_for_a_non_devnet_program_id(other_id):
    warns = _Warns()
    s = Settings.from_env(_env(other_id, "0"), warn=warns)
    assert s.holdback_days == C.HOLDBACK_DAYS == 7
    assert s.holdback_override_active is False
    assert len(warns.lines) == 1, warns.lines
    loud = warns.lines[0]
    assert "IGNORED" in loud
    assert C.HOLDBACK_OVERRIDE_ENV in loud
    assert other_id in loud
    assert "ENFORCED" in loud


def test_the_near_miss_id_really_differs_by_one_character():
    assert NEAR_MISS != DEVNET_A
    assert len(NEAR_MISS) == len(DEVNET_A)
    assert sum(a != b for a, b in zip(NEAR_MISS, DEVNET_A)) == 1


def test_a_non_devnet_id_cannot_be_given_a_non_seven_holdback_directly():
    """Even constructing Settings by hand cannot smuggle an override in."""
    with pytest.raises(PayoutConfigError) as exc:
        Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT, holdback_days=0)
    assert "devnet" in str(exc.value)
    # The same value IS allowed for a devnet id.
    assert Settings(program_id=DEVNET_A, life_mint=LIFE_MINT,
                    holdback_days=0).holdback_days == 0


# ════════════════════════════════════════════════════════════════════════════
# Refused at start-up: negative or not an integer
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("bad", ["-1", "-7", "abc", "0.0", "3.5", "seven",
                                 "true", "0x0", "1 2", "1,", "+-1", "٣"])
@pytest.mark.parametrize("program_id", [DEVNET_A, DEVNET_B, PROGRAM_ID])
def test_a_bad_override_refuses_at_start_up_on_every_network(bad, program_id):
    warns = _Warns()
    with pytest.raises(PayoutConfigError) as exc:
        Settings.from_env(_env(program_id, bad), warn=warns)
    msg = str(exc.value)
    assert C.HOLDBACK_OVERRIDE_ENV in msg
    assert "non-negative integer" in msg


def test_parse_holdback_override_rejects_bools_and_negatives():
    assert C.parse_holdback_override("0") == 0
    assert C.parse_holdback_override(7) == 7
    for bad in (-1, "-1", True, False, "1.0", "", "  ", None, "nine"):
        with pytest.raises(ValueError):
            C.parse_holdback_override(bad)


# ════════════════════════════════════════════════════════════════════════════
# Default: without the setting the constant stays 7
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("program_id", [DEVNET_A, DEVNET_B, PROGRAM_ID])
def test_without_the_setting_the_holdback_stays_seven(program_id):
    warns = _Warns()
    s = Settings.from_env(_env(program_id), warn=warns)
    assert s.holdback_days == 7
    assert s.holdback_override_active is False
    assert warns.lines == []


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_override_is_treated_as_unset(empty):
    s = Settings.from_env(_env(DEVNET_A, empty))
    assert s.holdback_days == 7


def test_the_plain_holdback_days_env_var_is_still_ignored():
    """Only HOLDBACK_DAYS_OVERRIDE can change anything — and only on devnet."""
    s = Settings.from_env({"PROGRAM_ID": DEVNET_A, "LIFE_MINT": LIFE_MINT,
                           "HOLDBACK_DAYS": "0"})
    assert s.holdback_days == 7


# ════════════════════════════════════════════════════════════════════════════
# The two rules it touches, consistently
# ════════════════════════════════════════════════════════════════════════════
def test_holdback_helpers_follow_the_effective_value():
    first_seen_ms = 1_728_000_000_000          # firstSeenDay 20000
    # Default / explicit 7: held through day 20006, released on 20007.
    assert C.is_held_on_day(first_seen_ms, 20002)
    assert C.is_held_on_day(first_seen_ms, 20002, 7)
    assert C.holdback_release_day(first_seen_ms) == 20007
    # With 0 there is no window at all: D < firstSeenDay + 0 is never true.
    assert not C.is_held_on_day(first_seen_ms, 20000, 0)
    assert not C.is_held_on_day(first_seen_ms, 20002, 0)
    assert C.holdback_release_day(first_seen_ms, 0) == 20000


def test_with_zero_there_is_no_first_week_to_forfeit():
    assert C.forfeiture_applies() is True
    assert C.forfeiture_applies(7) is True
    assert C.forfeiture_applies(1) is True
    assert C.forfeiture_applies(0) is False


def _findings_for(tampered_name: str, day: int, holdback_days: int,
                  log_source) -> set[str]:
    """Run ONE tampered fixture through the verifier at a given holdback."""
    from payout.recompute import compute_day
    from payout.verifier import Batch, verify_batch

    program_id = DEVNET_A if holdback_days != C.HOLDBACK_DAYS else PROGRAM_ID
    settings = Settings(program_id=program_id, life_mint=LIFE_MINT,
                        holdback_days=holdback_days)
    targets = TargetsTable.from_file(TARGETS_TABLE)
    dl = log_source.day_log(day)
    hist = [log_source.day_log(d) for d in VALID_DAYS]
    comp = compute_day(dl, targets=targets, history=hist,
                       holdback_days=holdback_days)
    res = verify_batch(Batch.from_json(load_tampered(tampered_name)), comp, dl,
                       anchored_hashes()[day], settings, targets,
                       signed_batch_ids=set())
    return {f.code for f in res.findings}


def test_the_held_wallet_rule_follows_the_override(log_source):
    """Bundle fixture 06 refuses with held_wallet_paid at 7, not at 0."""
    at_seven = _findings_for("06_held_wallet_paid", 20002, 7, log_source)
    assert HELD_WALLET_PAID in at_seven, sorted(at_seven)
    at_zero = _findings_for("06_held_wallet_paid", 20002, 0, log_source)
    assert HELD_WALLET_PAID not in at_zero, sorted(at_zero)


def test_the_forfeiture_rule_follows_the_override(log_source):
    """Bundle fixture 07 refuses with forfeited_wallet_paid at 7, not at 0.

    §5.1.5 forfeits FIRST-WEEK results.  With a holdback of 0 the same
    configuration says there is no first week, so the two rules must agree —
    otherwise a devnet batch would be refused for forfeiting an empty window.
    """
    at_seven = _findings_for("07_forfeited_wallet_paid", 20001, 7, log_source)
    assert FORFEITED_WALLET_PAID in at_seven, sorted(at_seven)
    at_zero = _findings_for("07_forfeited_wallet_paid", 20001, 0, log_source)
    assert FORFEITED_WALLET_PAID not in at_zero, sorted(at_zero)
    assert HELD_WALLET_PAID not in at_zero, sorted(at_zero)


def test_compute_day_skips_held_results_at_seven_but_pays_them_at_zero(
        log_source):
    """The recompute side moves in step with the verifier side."""
    from payout.recompute import compute_day
    targets = TargetsTable.from_file(TARGETS_TABLE)
    dl = log_source.day_log(20002)
    hist = [log_source.day_log(d) for d in VALID_DAYS]

    held7 = {rid: why for rid, why in
             compute_day(dl, targets=targets, history=hist,
                         holdback_days=7).skipped.items()
             if why.startswith("held:")}
    held0 = {rid: why for rid, why in
             compute_day(dl, targets=targets, history=hist,
                         holdback_days=0).skipped.items()
             if why.startswith("held:")}
    assert held0 == {}, held0
    # Default (no argument) must behave exactly like 7.
    heldD = {rid: why for rid, why in
             compute_day(dl, targets=targets, history=hist).skipped.items()
             if why.startswith("held:")}
    assert heldD == held7


# ════════════════════════════════════════════════════════════════════════════
# The CLI's start-up banner
# ════════════════════════════════════════════════════════════════════════════
def test_cli_prints_the_active_banner_only_for_a_devnet_id(capsys, tmp_path):
    """DEVNET HOLDBACK OVERRIDE ACTIVE, with the value and the program id."""
    from payout.cli import _build_service
    from payout.errors import PayoutError

    class _Args:
        coordinator = "http://127.0.0.1:1"
        timeout = 1.0

    env = _env(DEVNET_A, "0")
    env["TARGETS_TABLE"] = str(TARGETS_TABLE)
    # Deliberately stop at the NEXT required setting: the banner is printed
    # before any of it, which is the point — it cannot be missed.
    with pytest.raises(PayoutError):
        _build_service(_Args(), env)
    out = capsys.readouterr()
    banner = out.out + out.err
    assert "DEVNET HOLDBACK OVERRIDE ACTIVE" in banner, banner
    assert "HOLDBACK_DAYS=0" in banner
    assert DEVNET_A in banner


def test_cli_prints_the_ignored_warning_for_a_non_devnet_id(capsys):
    from payout.cli import _build_service
    from payout.errors import PayoutError

    class _Args:
        coordinator = "http://127.0.0.1:1"
        timeout = 1.0

    env = _env(PROGRAM_ID, "0")
    env["TARGETS_TABLE"] = str(TARGETS_TABLE)
    with pytest.raises(PayoutError):
        _build_service(_Args(), env)
    out = capsys.readouterr()
    text = out.out + out.err
    assert "IGNORED" in text, text
    assert "DEVNET HOLDBACK OVERRIDE ACTIVE" not in text
