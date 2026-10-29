"""Fixtures for the payout-signer suite.

Throwaway keys only: generated in memory, written to a pytest tmp_path, never
to any real wallet location.  No chain, no PM2 process, no live coordinator.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from solders.keypair import Keypair

from mock_payout_coordinator import MockPayoutCoordinator, anchored_hashes
from payout.ledger import DecisionLedger
from payout.signer_key import PayoutSigner
from payout.sources import AnchorSource, LogSource, OperatorInputs

# Day 20000 = 2024-10-04. The fixtures' results were submitted well before
# that (feed submitted_at 1726790400000 = 2024-09-20), so every wallet is far
# past the 7-day holdback at this instant.
NOW_MS = 1_728_172_800_000  # 2024-10-06T00:00:00Z, the fixtures' builtAt

# Targets appearing in the fixtures. CDK4/MYC are named overrides in §6.1;
# EGFR and TP53 need an explicit modality — the public log has no modality.
OPERATOR_TARGET_MODALITY = {
    "CDK4": "protein",
    "MYC": "protein",
    "EGFR": "protein",
    "TP53": "small_molecule",
}

FLAGGED_WALLET = "9wLoojm7k8DrPgikDH1mEyis9F3oKcsJiJVqFewJu4b1"


def write_throwaway_keypair(path: Path) -> str:
    """Write a throwaway 64-byte Solana keypair file; return its base58 pubkey."""
    kp = Keypair()
    path.write_text(json.dumps(list(bytes(kp))))
    return str(kp.pubkey())


@pytest.fixture
def coordinator():
    with MockPayoutCoordinator() as mock:
        yield mock


@pytest.fixture
def log_source(coordinator) -> LogSource:
    return LogSource(coordinator.url, timeout=5.0)


@pytest.fixture
def anchors() -> AnchorSource:
    return AnchorSource(anchored_hashes())


@pytest.fixture
def operator() -> OperatorInputs:
    return OperatorInputs(
        target_modality=dict(OPERATOR_TARGET_MODALITY),
        flagged_wallets={FLAGGED_WALLET},
        forfeited_result_ids=set(),
        source_path="<test fixture>",
    )


@pytest.fixture
def ledger(tmp_path) -> DecisionLedger:
    return DecisionLedger(tmp_path / "state")


@pytest.fixture
def payout_key_paths(tmp_path) -> dict[str, str]:
    """Two DISTINCT throwaway keys: payout and validator-verdict."""
    payout = tmp_path / "payout_throwaway.json"
    validator = tmp_path / "validator_throwaway.json"
    write_throwaway_keypair(payout)
    write_throwaway_keypair(validator)
    return {"PAYOUT_KEYPAIR": str(payout), "VALIDATOR_KEYPAIR": str(validator)}


@pytest.fixture
def signer(payout_key_paths) -> PayoutSigner:
    return PayoutSigner.from_env(payout_key_paths)


@pytest.fixture
def service(log_source, anchors, operator, ledger, signer):
    from payout.service import PayoutSignerService
    return PayoutSignerService(log_source, anchors, operator, ledger, signer)
