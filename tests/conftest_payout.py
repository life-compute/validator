"""Fixtures for the payout-signer suite.

Throwaway keys only: generated in memory, written to a pytest tmp_path, never
to any real wallet location.  No chain, no PM2 process, no live coordinator.

The anchored hashes are a MOCK (mock_payout_coordinator.anchored_hashes), so
the wrong-anchor and repeated-batch-id variants are testable without a chain.

C5b4: there is no checker clock.  NOW_MS and the hand-picked "feasible
holdback window" are gone: BATCH_SPEC §2/§5.1.3 make holdback a function of
the batch's day number and the log's own wallet_first_seen event, and §5.0
now fires `forfeited_wallet_paid` (11) before `held_wallet_paid` (12), so no
fixture needs a particular instant to reach its published rule.  Tests that
care prove this by running the whole set under three different fake clocks.
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

# ── system clocks the whole fixture set must decide identically under ───────
# Task 2: the checker reads no clock, so these are only ever installed to
# PROVE that.  They straddle the fixture dates on both sides (2024 is around
# them, 2026 and 2030 are years later).
FAKE_CLOCKS_MS = {
    "2024": 1_704_067_200_000,      # 2024-01-01T00:00:00Z
    "2026": 1_767_225_600_000,      # 2026-01-01T00:00:00Z
    "2030": 1_893_456_000_000,      # 2030-01-01T00:00:00Z
}

# BATCH_SPEC §16 / README: the required checker configuration.
PROGRAM_ID = "BU7WVCvy9CqypB7Hqhw6RJakAeSieVsWNapytQpFgi18"
LIFE_MINT = "CVDFLCAjXhVWiPXH9nTCTpCgVzmDVoiPzNJYuccr1dqB"


def write_throwaway_keypair(path: Path) -> str:
    """Write a throwaway 64-byte Solana keypair file; return its base58 pubkey.

    Mode 0600: the start-up guard refuses any payout key that is readable by
    someone other than its owner, so the fixtures must honour it too.
    """
    kp = Keypair()
    path.write_text(json.dumps(list(bytes(kp))))
    path.chmod(0o600)
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


@pytest.fixture
def fake_clock(monkeypatch):
    """Install a fixed system clock; the checker must not notice.

    Patches `time.time`, `time.time_ns` and `datetime.datetime.now/utcnow` so
    that ANY wall-clock call made anywhere under test returns the chosen
    instant.  The checker is supposed to make none.
    """
    import datetime as _dt
    import time as _time

    def _install(ms: int):
        secs = ms / 1000.0
        monkeypatch.setattr(_time, "time", lambda: secs)
        monkeypatch.setattr(_time, "time_ns", lambda: int(ms) * 1_000_000)

        fixed = _dt.datetime.fromtimestamp(secs, _dt.timezone.utc)

        class _FrozenDateTime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

            @classmethod
            def utcnow(cls):
                return fixed.replace(tzinfo=None)

            @classmethod
            def today(cls):
                return fixed.replace(tzinfo=None)

        monkeypatch.setattr(_dt, "datetime", _FrozenDateTime)
        return ms

    return _install
