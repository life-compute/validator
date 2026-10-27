"""Per-GPU bias table, built from the old audit log.

ATTRIBUTION CUTOFF
------------------
Before 2026-09-18T12:00Z the `miner_gpu` field in validator_audit.jsonl
recorded the VALIDATOR's own card, not the submitting miner's hardware — two
different miners were both filed under "RTX 4060".  Those rows cannot be
re-attributed, so every table built here ignores anything older.

The factor is the mean of rescored/claimed over post-cutoff rows for a
(gpu_model, modality) bucket with at least `min_samples` observations, and is
only applied when it lands inside [BIAS_FACTOR_LO, BIAS_FACTOR_HI].
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("life-validator.bias")

# Audit `target_type` → contract modality.
_TARGET_TYPE_TO_MODALITY = {
    "PROTEIN": "protein",
    "RNA": "mrna",
    "CRISPR": "crispr",
    "SMALL_MOLECULE": "small_molecule",
}

UNATTRIBUTED_PREFIX = "UNATTRIBUTED_PRE_"


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def build_table(audit_path: str | Path, cutoff_utc: str, *,
                min_samples: int = 10) -> dict[str, dict[str, Any]]:
    """{gpu_model: {modality: {n, bias_factor}}} from post-cutoff rows only."""
    cutoff = _parse_ts(cutoff_utc)
    if cutoff is None:
        raise ValueError(f"unparseable cutoff {cutoff_utc!r}")

    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    skipped_precutoff = 0
    path = Path(audit_path)
    if not path.exists():
        log.warning("bias: audit log %s not found — no bias table", path)
        return {}

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue

            ts = _parse_ts(row.get("ts", ""))
            if ts is None:
                continue
            if ts < cutoff:
                skipped_precutoff += 1
                continue

            gpu = row.get("miner_gpu") or ""
            if not gpu or gpu == "UNKNOWN" or gpu.startswith(UNATTRIBUTED_PREFIX):
                continue

            claimed = row.get("claimed_score")
            rescored = row.get("rescored")
            if claimed in (None, 0) or rescored is None:
                continue

            modality = _TARGET_TYPE_TO_MODALITY.get(row.get("target_type") or "")
            if modality is None:
                continue

            # Cross-formula guard: early mRNA rows carry a tiny claimed value
            # from a superseded formula and give a ratio in the hundreds.
            if modality == "mrna" and abs(claimed) < 1.0:
                continue

            buckets[(gpu, modality)].append(rescored / claimed)

    table: dict[str, dict[str, Any]] = {}
    for (gpu, modality), ratios in buckets.items():
        if len(ratios) < min_samples:
            continue
        table.setdefault(gpu, {})[modality] = {
            "n": len(ratios),
            "bias_factor": round(sum(ratios) / len(ratios), 6),
        }

    log.info("bias: built table from rows at/after %s (%d pre-cutoff rows ignored); "
             "%d GPU bucket(s)", cutoff_utc, skipped_precutoff, len(table))
    return table


class BiasTable:
    def __init__(self, table: dict[str, dict[str, Any]], *,
                 lo: float = 0.1, hi: float = 10.0):
        self._t = table
        self.lo = lo
        self.hi = hi

    @classmethod
    def from_audit(cls, audit_path: str | Path, cutoff_utc: str, *,
                   min_samples: int = 10, lo: float = 0.1, hi: float = 10.0) -> "BiasTable":
        return cls(build_table(audit_path, cutoff_utc, min_samples=min_samples),
                   lo=lo, hi=hi)

    @classmethod
    def from_json(cls, path: str | Path, *, lo: float = 0.1, hi: float = 10.0) -> "BiasTable":
        data = json.loads(Path(path).read_text())
        return cls(data.get("table", data), lo=lo, hi=hi)

    def factor(self, gpu_model: str, modality: str) -> float | None:
        """Applicable bias factor, or None when there is none to apply.

        Per the contract, bias correction is protein-only.
        """
        if modality != "protein":
            return None
        if not gpu_model or gpu_model == "UNKNOWN":
            return None
        entry = self._t.get(gpu_model, {}).get(modality)
        if not entry:
            return None
        bf = entry.get("bias_factor")
        if bf is None or not (self.lo <= bf <= self.hi):
            log.warning("bias: factor %r for %s/%s outside [%s,%s] — ignoring",
                        bf, gpu_model, modality, self.lo, self.hi)
            return None
        return float(bf)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return dict(self._t)
