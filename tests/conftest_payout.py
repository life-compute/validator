"""Fixtures for the payout-signer suite.

Throwaway keys only: generated in memory, written to a pytest tmp_path, never
to any real wallet location.  No chain, no PM2 process, no live coordinator.

The anchored hashes are a MOCK (mock_payout_coordinator.anchored_hashes), so
the wrong-anchor and repeated-batch-id variants are testable without a chain.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from solders.keypair import Keypair

from mock_payout_coordinator import (
    MockPayoutCoordinator,
    TARGETS_TABLE,
    VALID_DAYS,
    anchored_hashes,
)
from payout.ledger import DecisionLedger
from payout.signer_key import PayoutSigner
from payout.sources import AnchorSource, LogSource, Settings, TargetsTable

# ── the checker's clock (§5.1.3 holdback is measured against it) ────────────
#
# NOW_MS is NOT free to choose: BATCH_SPEC §5.0 orders held_wallet_paid (11)
# BEFORE forfeited_wallet_paid (12), so a wallet that is both held and
# forfeited reports *held*.  Two fixtures pull in opposite directions:
#
#   07_forfeited_wallet_paid  BbTNoDCY… first_seen 1727740800000, forfeited.
#       To surface `forfeited_wallet_paid` it must NOT also be held:
#           nowMs ≥ 1727740800000 + 604800000 = 1728345600000
#   06_held_wallet_paid       ATQisH1y… first_seen 1728000000000, not forfeited.
#       To surface `held_wallet_paid` it must still be inside the window:
#           nowMs <  1728000000000 + 604800000 = 1728604800000
#
# Feasible window: [1728345600000, 1728604800000)
#                  = 2024-10-08T00:00Z .. 2024-10-11T00:00Z (exclusive).
# The batches' own builtAt (1728172800000, 2024-10-06) is OUTSIDE it — at that
# instant BbTNoDCY is still inside its first week and correctly reports
# `held_wallet_paid`, not the rule its verdict file names.  We therefore run
# the checker at 2024-10-09T00:00Z, the midpoint of the feasible window.
#
# Wallets first seen 1726790400000 (2024-09-20) are far past the holdback at
# this instant, so the three valid days are unaffected.
NOW_MS = 1_728_432_000_000          # 2024-10-09T00:00:00Z

# The window above, asserted in tests so a future clock change cannot silently
# flip 06 and 07 to the wrong rule.
HOLDBACK_WINDOW_MIN_MS = 1_728_345_600_000
HOLDBACK_WINDOW_MAX_MS = 1_728_604_800_000

# BATCH_SPEC §16 / README: the required checker configuration.
PROGRAM_ID = "BU7WVCvy9CqypB7Hqhw6RJakAeSieVsWNapytQpFgi18"
LIFE_MINT = "CVDFLCAjXhVWiPXH9nTCTpCgVzmDVoiPzNJYuccr1dqB"


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
def settings() -> Settings:
    return Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT)


@pytest.fixture
def targets() -> TargetsTable:
    """The real bundle table, SHA-256-verified against BATCH_SPEC §12."""
    return TargetsTable.from_file(TARGETS_TABLE)


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
def service(log_source, anchors, settings, targets, ledger, signer):
    from payout.service import PayoutSignerService
    return PayoutSignerService(log_source, anchors, settings, targets,
                               ledger, signer)


@pytest.fixture
def history_days() -> tuple[int, ...]:
    """All valid fixture days: wallet_first_seen is emitted once, on day 0."""
    return VALID_DAYS
