"""Per-GPU bias table tests — the attribution cutoff is the point.

Before 2026-09-18T12:00Z the `miner_gpu` field recorded the VALIDATOR's own
card, not the miner's.  Any table that includes those rows is wrong.
"""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_client.bias import BiasTable, build_table

CUTOFF = "2026-09-18T12:00:00+00:00"


def _audit(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "audit.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _row(ts: str, gpu: str, tt: str, claimed: float, rescored: float) -> dict:
    return {"ts": ts, "miner_gpu": gpu, "target_type": tt,
            "claimed_score": claimed, "rescored": rescored}


def test_pre_cutoff_rows_are_ignored(tmp_path):
    """20 pre-cutoff rows must not produce a bucket; the cutoff is the gate."""
    rows = [_row("2026-09-01T00:00:00+00:00", "RTX 4060", "PROTEIN", -8.0, -4.0)
            for _ in range(20)]
    table = build_table(_audit(tmp_path, rows), CUTOFF, min_samples=10)
    assert table == {}, "pre-cutoff rows leaked into the bias table"


def test_post_cutoff_rows_build_a_bucket(tmp_path):
    rows = [_row("2026-09-19T00:00:00+00:00", "RTX 5060", "PROTEIN", -8.0, -7.6)
            for _ in range(12)]
    table = build_table(_audit(tmp_path, rows), CUTOFF, min_samples=10)
    assert table["RTX 5060"]["protein"]["n"] == 12
    assert abs(table["RTX 5060"]["protein"]["bias_factor"] - 0.95) < 1e-6


def test_mixed_rows_use_only_the_post_cutoff_half(tmp_path):
    """A pre-cutoff bias of 0.5 must not drag the post-cutoff 0.95 factor."""
    rows = ([_row("2026-09-01T00:00:00+00:00", "RTX 5060", "PROTEIN", -8.0, -4.0)] * 50
            + [_row("2026-09-19T00:00:00+00:00", "RTX 5060", "PROTEIN", -8.0, -7.6)] * 12)
    table = build_table(_audit(tmp_path, rows), CUTOFF, min_samples=10)
    assert table["RTX 5060"]["protein"]["n"] == 12
    assert abs(table["RTX 5060"]["protein"]["bias_factor"] - 0.95) < 1e-6


def test_boundary_row_at_exact_cutoff_is_included(tmp_path):
    rows = [_row("2026-09-18T12:00:00+00:00", "RTX 5060", "PROTEIN", -8.0, -7.6)
            for _ in range(10)]
    table = build_table(_audit(tmp_path, rows), CUTOFF, min_samples=10)
    assert table["RTX 5060"]["protein"]["n"] == 10


def test_below_min_samples_yields_no_factor(tmp_path):
    rows = [_row("2026-09-19T00:00:00+00:00", "RTX 5060", "PROTEIN", -8.0, -7.6)
            for _ in range(9)]
    assert build_table(_audit(tmp_path, rows), CUTOFF, min_samples=10) == {}


def test_unknown_and_unattributed_gpus_are_dropped(tmp_path):
    rows = ([_row("2026-09-19T00:00:00+00:00", "UNKNOWN", "PROTEIN", -8.0, -7.6)] * 20
            + [_row("2026-09-19T00:00:00+00:00", "UNATTRIBUTED_PRE_RTX 4060",
                    "PROTEIN", -8.0, -7.6)] * 20)
    assert build_table(_audit(tmp_path, rows), CUTOFF, min_samples=10) == {}


def test_tiny_claimed_mrna_rows_are_skipped(tmp_path):
    """Cross-formula mRNA rows (|claimed| < 1) give ratios in the hundreds."""
    rows = [_row("2026-09-19T00:00:00+00:00", "RTX 5060", "RNA", -0.028, -7.8)
            for _ in range(20)]
    assert build_table(_audit(tmp_path, rows), CUTOFF, min_samples=10) == {}


def test_bias_is_protein_only():
    """Contract §3.2 step 3: bias adjustment applies to protein only."""
    t = BiasTable({"RTX 5060": {"protein": {"n": 20, "bias_factor": 0.95},
                                "crispr": {"n": 20, "bias_factor": 0.88}}})
    assert t.factor("RTX 5060", "protein") == 0.95
    assert t.factor("RTX 5060", "crispr") is None
    assert t.factor("RTX 5060", "mrna") is None


def test_out_of_range_factor_is_ignored():
    t = BiasTable({"GPU": {"protein": {"n": 20, "bias_factor": 42.0}}}, lo=0.1, hi=10.0)
    assert t.factor("GPU", "protein") is None


def test_unknown_gpu_gets_no_factor():
    t = BiasTable({"RTX 5060": {"protein": {"n": 20, "bias_factor": 0.95}}})
    assert t.factor("", "protein") is None
    assert t.factor("UNKNOWN", "protein") is None
    assert t.factor("RTX 9090", "protein") is None


def test_real_audit_table_has_only_post_cutoff_buckets():
    """The checked-in table was generated with the cutoff applied."""
    p = Path(__file__).parent / "data" / "gpu_bias_postcutoff.json"
    data = json.loads(p.read_text())
    assert data["cutoff_utc"] == "2026-09-18T12:00:00Z"
    for gpu, mods in data["table"].items():
        assert not gpu.startswith("UNATTRIBUTED_PRE_")
        for _m, entry in mods.items():
            assert entry["n"] >= 10
