"""Replay harness: feed the old validator_audit.jsonl through the new code.

For every historical row we re-derive the verdict with the NEW decision path
(coordinator tolerances, post-cutoff per-GPU bias, contract rel_err) using
the recorded claimed/rescored pair, and compare against the decision the old
daemon actually recorded.

This replays DECISIONS, not Boltz runs: the stored `rescored` value is used
as the re-score, so no GPU work happens.

Divergences are expected and are the point of the harness — the old daemon
used a local devnet tolerance of 0.7777 while the coordinator specifies
0.35/0.25 (protein) and 0.10 (mrna/crispr).  The test asserts that every
divergence is EXPLAINED by a tolerance change, never unexplained.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_client import contract
from coordinator_client.bias import BiasTable

AUDIT = Path(os.environ.get("REPLAY_AUDIT", "/tmp/validator/output/validator_audit.jsonl"))
CUTOFF = "2026-09-18T12:00:00+00:00"
BIAS_JSON = Path(__file__).parent / "data" / "gpu_bias_postcutoff.json"

_TARGET_TYPE_TO_MODALITY = {"PROTEIN": "protein", "RNA": "mrna",
                            "CRISPR": "crispr", "SMALL_MOLECULE": "small_molecule"}

# The old daemon's decision strings, normalised to confirm/reject.
_OLD_CONFIRM = {"CONFIRM", "CONFIRM_TX_FAILED"}
_OLD_REJECT = {"REJECT", "REJECT_TX_FAILED"}


def _load_rows(limit: int | None = None) -> list[dict]:
    if not AUDIT.exists():
        return []
    rows = []
    with AUDIT.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("ts", "") < CUTOFF[:16]:
                continue           # per-GPU attribution cutoff
            if row.get("decision") not in (_OLD_CONFIRM | _OLD_REJECT):
                continue
            if row.get("claimed_score") in (None, 0) or row.get("rescored") is None:
                continue
            if _TARGET_TYPE_TO_MODALITY.get(row.get("target_type") or "") is None:
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def replay_row(row: dict, cfg: dict, bias: BiasTable) -> dict:
    """Re-derive the verdict for one historical row with the new code."""
    modality = _TARGET_TYPE_TO_MODALITY[row["target_type"]]
    claimed = float(row["claimed_score"])
    rescored = float(row["rescored"])
    gpu = row.get("miner_gpu") or ""

    bias_factor = bias.factor(gpu, modality)
    adjusted = claimed * bias_factor if bias_factor is not None else claimed
    tolerance = contract.tolerance_for(cfg, modality, bias_factor is not None)
    decision, rel_err = contract.coord_decision(rescored, adjusted, tolerance)

    old = "confirm" if row["decision"] in _OLD_CONFIRM else "reject"
    return {"modality": modality, "old": old, "new": decision,
            "rel_err": rel_err, "tolerance": tolerance,
            "old_tolerance": row.get("tolerance_used"),
            "bias_factor": bias_factor, "claimed": claimed,
            "rescored": rescored}


@pytest.fixture(scope="module")
def replay_rows():
    rows = _load_rows(limit=4000)
    if not rows:
        pytest.skip(f"no replayable audit rows at {AUDIT}")
    return rows


@pytest.fixture(scope="module")
def bias_table():
    if BIAS_JSON.exists():
        return BiasTable.from_json(BIAS_JSON)
    return BiasTable({})


@pytest.mark.replay
def test_replay_produces_a_verdict_for_every_row(replay_rows, bias_table):
    cfg = dict(contract.DEFAULT_CONFIG)
    for row in replay_rows:
        out = replay_row(row, cfg, bias_table)
        assert out["new"] in ("confirm", "reject")
        assert out["rel_err"] >= 0.0


@pytest.mark.replay
def test_replay_divergences_are_all_explained(replay_rows, bias_table):
    """Any old-vs-new disagreement must have a known cause, not a bug.

    Two legitimate causes:

    1. TOLERANCE — the coordinator's tolerance differs from the local devnet
       one the old daemon used, and rel_err falls between the two.
    2. BIAS AVAILABILITY — the old daemon learned bias factors from the WHOLE
       audit history, including pre-2026-09-18T12:00Z rows where `miner_gpu`
       actually recorded the validator's own card.  Rebuilding the table from
       post-cutoff rows only means some buckets no longer reach the 10-sample
       floor, so no factor applies and `adjusted_claimed` changes.  That is
       the cutoff rule working as intended, not a regression.

    Anything else means the arithmetic itself changed — that is a failure.
    """
    cfg = dict(contract.DEFAULT_CONFIG)
    unexplained = []
    causes = {"tolerance": 0, "bias_availability": 0}
    diverged = 0

    for row in replay_rows:
        out = replay_row(row, cfg, bias_table)
        if out["old"] == out["new"]:
            continue
        diverged += 1

        old_bias = row.get("bias_factor_used", row.get("bias_factor"))
        bias_changed = (old_bias is not None) != (out["bias_factor"] is not None)
        if bias_changed:
            causes["bias_availability"] += 1
            continue

        old_tol = out["old_tolerance"]
        if old_tol is not None:
            lo, hi = sorted((float(old_tol), float(out["tolerance"])))
            if lo <= out["rel_err"] <= hi:
                causes["tolerance"] += 1
                continue

        unexplained.append(out)

    assert not unexplained, (
        f"{len(unexplained)}/{diverged} divergences are NOT explained by a "
        f"tolerance or bias-availability change (causes so far: {causes}), "
        f"e.g. {unexplained[:3]}")


@pytest.mark.replay
def test_old_daemon_decisions_were_internally_consistent(replay_rows):
    """Sanity-check the replay INPUT: every recorded decision must match its
    own recorded rel_err vs tolerance_used.  If this fails, the audit log is
    not a trustworthy baseline to compare against."""
    bad = []
    for row in replay_rows:
        rel_err = row.get("rel_err")
        tol = row.get("tolerance_used")
        if rel_err is None or tol is None:
            continue
        expected = "confirm" if rel_err <= tol else "reject"
        actual = "confirm" if row["decision"] in _OLD_CONFIRM else "reject"
        if expected != actual:
            bad.append(row)
    assert not bad, f"{len(bad)} audit rows contradict their own rel_err/tolerance"


@pytest.mark.replay
def test_replay_is_deterministic(replay_rows, bias_table):
    cfg = dict(contract.DEFAULT_CONFIG)
    first = [replay_row(r, cfg, bias_table)["new"] for r in replay_rows[:500]]
    second = [replay_row(r, cfg, bias_table)["new"] for r in replay_rows[:500]]
    assert first == second


@pytest.mark.replay
def test_replay_tightening_only_moves_confirm_to_reject(replay_rows, bias_table):
    """Coordinator tolerances are tighter, so no row may newly CONFIRM.

    Exception: protein rows where a post-cutoff bias factor now applies can
    legitimately move either way, because the claimed value is rescaled.
    """
    cfg = dict(contract.DEFAULT_CONFIG)
    bad = []
    for row in replay_rows:
        out = replay_row(row, cfg, bias_table)
        if out["old"] == "reject" and out["new"] == "confirm":
            if out["bias_factor"] is None:
                bad.append(out)
    assert not bad, f"{len(bad)} rows loosened without a bias factor: {bad[:3]}"


@pytest.mark.replay
def test_replay_summary(replay_rows, bias_table, capsys):
    """Emit the comparison table — this is the harness's actual output."""
    cfg = dict(contract.DEFAULT_CONFIG)
    from collections import Counter

    agree: Counter = Counter()
    flip: Counter = Counter()
    for row in replay_rows:
        out = replay_row(row, cfg, bias_table)
        key = out["modality"]
        if out["old"] == out["new"]:
            agree[key] += 1
        else:
            flip[f"{key}:{out['old']}->{out['new']}"] += 1

    total = sum(agree.values()) + sum(flip.values())
    with capsys.disabled():
        print(f"\n  replay: {total} audit rows at/after {CUTOFF}")
        for k, v in sorted(agree.items()):
            print(f"    agree  {k:<16} {v:>6}")
        for k, v in sorted(flip.items()):
            print(f"    FLIP   {k:<16} {v:>6}")
    assert total > 0
