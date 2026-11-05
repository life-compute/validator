"""C47 — decoding the REAL on-chain accounts with the program's Rust layouts.

The layouts come from `programs/core/src/state/*.rs` as copied into the C47
spec bundle; this file proves they decode the live devnet accounts of the two
wallets the task names:

    miner      G8wkwTUC4nyQcaxDtRPPBeevS5pXque5hdExrNrhNa5L
    validator  493jnXXvyQyjaSxeWeRGBcoXnokoUvMmfiJxr1V5i1Uo

`tests/data/devnet_accounts_c47.json` holds the exact base64 account data
read from devnet with a read-only `getAccountInfo` / `getProgramAccounts`, so
the assertions below are deterministic and run with no network.  The
`live` test at the end repeats the read against devnet and is skipped unless
C47_LIVE_RPC=1, so an offline or rate-limited run never fails spuriously.

Nothing here signs, posts or writes anything on chain.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from payout.chain_state import (
    MINER_ACCOUNT_LEN,
    NETWORK_CONFIG_LEN,
    ChainRegistry,
    ChainStateError,
    ManifestRegistry,
    MinerAccount,
    NetworkConfig,
    miner_pda,
    validator_account_pda,
)

DATA = Path(__file__).parent / "data" / "devnet_accounts_c47.json"

pytestmark = pytest.mark.skipif(
    not DATA.is_file(), reason="the recorded devnet account fixture is absent")


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(DATA.read_text())


# ── the PDA derivation ──────────────────────────────────────────────────────
def test_the_miner_pda_is_derived_with_the_program_id(fixture):
    """seeds = [b"miner", wallet]; the PDA must match the one that was read."""
    assert miner_pda(fixture["miner_wallet"],
                     fixture["program_id"]) == fixture["miner_pda"]


def test_a_different_program_id_gives_a_different_pda(fixture):
    other = "BU7WVCvy9CqypB7Hqhw6RJakAeSieVsWNapytQpFgi18"
    assert miner_pda(fixture["miner_wallet"], other) != fixture["miner_pda"]


def test_the_validator_account_pda_derives(fixture):
    pda = validator_account_pda(fixture["validator_wallet"],
                                fixture["program_id"])
    assert isinstance(pda, str) and 32 <= len(pda) <= 44


# ── MinerAccount, decoded from the REAL bytes ───────────────────────────────
def test_the_real_miner_account_decodes(fixture):
    raw = base64.b64decode(fixture["miner_account_b64"])
    assert len(raw) == MINER_ACCOUNT_LEN == 128     # miner_account.rs LEN
    acct = MinerAccount.decode(raw)
    # owner at [8..39] is the wallet itself — the single strongest check that
    # the offsets are right: a wrong offset cannot produce this base58 string.
    assert acct.owner == fixture["miner_wallet"]
    assert acct.is_registered is True
    assert 0 <= acct.bump <= 255
    # Every remaining field decodes to a sane type/range.
    assert isinstance(acct.total_life_earned, int) and acct.total_life_earned >= 0
    assert acct.molecules_screened >= 0
    assert acct.last_epoch >= 0
    assert acct.submission_count <= 255
    assert acct.submission_epoch >= 0
    assert acct.stake_returned in (True, False)
    assert acct.failed_verification in (True, False)
    assert acct.open_dispute in (True, False)
    assert acct.stake_locked_until_slot >= 0


def test_a_wrong_length_account_is_refused():
    with pytest.raises(ChainStateError) as exc:
        MinerAccount.decode(b"\x00" * 127)
    assert "128 bytes" in str(exc.value)


# ── NetworkConfig, decoded from the REAL bytes ──────────────────────────────
def test_the_real_network_config_decodes(fixture):
    raw = base64.b64decode(fixture["network_config_b64"])
    assert len(raw) == NETWORK_CONFIG_LEN == 344    # network_config.rs LEN
    nc = NetworkConfig.decode(raw)
    assert nc.validator_count <= 5
    assert nc.validators_required >= 1
    assert 0.0 <= nc.validation_tolerance <= 10.0
    assert len(nc.active_validators()) == nc.validator_count
    for wallet in nc.active_validators():
        assert 32 <= len(wallet) <= 44


def test_the_validator_wallet_is_in_the_on_chain_set(fixture):
    """`is_validator` is the program's own rule: only the first
    `validator_count` slots of the array count."""
    nc = NetworkConfig.decode(base64.b64decode(fixture["network_config_b64"]))
    assert nc.is_validator(fixture["validator_wallet"]), (
        f"{fixture['validator_wallet']} not in {nc.active_validators()}")


def test_a_wallet_outside_the_active_slots_is_not_a_validator(fixture):
    nc = NetworkConfig.decode(base64.b64decode(fixture["network_config_b64"]))
    assert not nc.is_validator("11111111111111111111111111111112")
    assert not nc.is_validator(fixture["miner_wallet"])


def test_stale_keys_past_validator_count_do_not_count():
    """A key left in a later slot is NOT a member — the Rust slices first."""
    nc = NetworkConfig(authority="a", life_mint="m", validators_required=2,
                       validation_tolerance=0.1,
                       validators=("A", "B", "STALE", "", ""),
                       validator_count=2, total_miners_registered=0,
                       total_validators_registered=0)
    assert nc.is_validator("A") and nc.is_validator("B")
    assert not nc.is_validator("STALE")


# ── the registry interface, driven by the recorded accounts ─────────────────
class _RecordedRpc:
    """Answers the two calls ChainRegistry makes, from the fixture only."""

    def __init__(self, fixture: dict):
        self.f = fixture
        self.calls: list[str] = []

    def call(self, method: str, params):
        self.calls.append(method)
        if method == "getAccountInfo":
            if params[0] == self.f["miner_pda"]:
                return {"value": {"data": [self.f["miner_account_b64"],
                                           "base64"]}}
            return {"value": None}
        if method == "getProgramAccounts":
            return [{"pubkey": self.f["network_config_pubkey"],
                     "account": {"data": [self.f["network_config_b64"],
                                          "base64"]}}]
        raise AssertionError(f"unexpected RPC method {method}")


def test_the_registry_answers_both_questions_from_the_chain(fixture):
    rpc = _RecordedRpc(fixture)
    reg = ChainRegistry(fixture["program_id"], rpc=rpc)
    assert reg.is_registered_miner(fixture["miner_wallet"]) is True
    assert reg.is_validator_in_set(fixture["validator_wallet"]) is True
    # A wallet with no miner PDA is simply not registered — never an error.
    assert reg.is_registered_miner("11111111111111111111111111111112") is False
    assert reg.is_validator_in_set("11111111111111111111111111111112") is False


def test_the_registry_asks_the_rpc_once_per_wallet(fixture):
    rpc = _RecordedRpc(fixture)
    reg = ChainRegistry(fixture["program_id"], rpc=rpc)
    for _ in range(5):
        reg.is_registered_miner(fixture["miner_wallet"])
        reg.is_validator_in_set(fixture["validator_wallet"])
    assert rpc.calls.count("getAccountInfo") == 1
    assert rpc.calls.count("getProgramAccounts") == 1


def test_the_manifest_registry_is_the_golden_substitute():
    """§17 golden README step 1: the golden vectors use the manifest list."""
    reg = ManifestRegistry([
        {"wallet": "M1", "role": "miner", "is_validator": False},
        {"wallet": "M2", "role": "miner+validator", "is_validator": True},
        {"wallet": "V1", "role": "validator", "is_validator": True},
    ])
    assert reg.is_registered_miner("M1") and not reg.is_validator_in_set("M1")
    assert reg.is_registered_miner("M2") and reg.is_validator_in_set("M2")
    assert reg.is_validator_in_set("V1") and not reg.is_registered_miner("V1")
    assert not reg.is_registered_miner("nobody")


# ── the live read (opt-in) ──────────────────────────────────────────────────
@pytest.mark.skipif(os.environ.get("C47_LIVE_RPC") != "1",
                    reason="set C47_LIVE_RPC=1 to read devnet in the suite")
def test_the_same_decoding_holds_against_live_devnet(fixture):
    reg = ChainRegistry(fixture["program_id"])
    assert reg.is_registered_miner(fixture["miner_wallet"]) is True
    assert reg.is_validator_in_set(fixture["validator_wallet"]) is True
    acct = reg.miner_account(fixture["miner_wallet"])
    assert acct is not None and acct.owner == fixture["miner_wallet"]
