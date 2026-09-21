#!/usr/bin/env python3
"""
peg_json_eval.py — validator-side INDEPENDENT evaluator for peg_model.json.

Written from the formula string declared in peg_spec.json / peg_model.json.
It deliberately does NOT import the miner's scripts.export_peg_model.predict_json:
reusing the miner's evaluator would make a symmetry check circular — it would
only prove that one function agrees with itself.  This is a separate
implementation of the same declared contract, which is what makes agreement
meaningful.

Stdlib only: json + struct.  No sklearn, no numpy, no version coupling.

Declared contract (verbatim from peg_model.json["formula"]):
    y_raw = init_constant + learning_rate * sum(tree_value(x) for tree in trees)
    y     = clamp(y_raw, 0.0, 1.0)
  Each tree: descend from node 0, take children_left when
    float32(x[feature[n]]) <= threshold[n] else children_right,
  stop when children_left[n] == -1, emit value[n].

float32 note (MANDATORY, from the spec): sklearn casts X to float32 while
thresholds stay float64.  Comparing in float64 takes a different branch near
rounding boundaries and silently changes the prediction — exactly the class of
silent divergence this whole stage exists to prevent.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path


def _f32(v: float) -> float:
    """Cast to IEEE-754 single precision, as sklearn does to X."""
    return struct.unpack("f", struct.pack("f", v))[0]


class PegJsonModel:
    def __init__(self, path: str | Path):
        with open(path) as fh:
            m = json.load(fh)
        if m.get("format") != "life-peg-gbr-json":
            raise ValueError(f"unexpected model format: {m.get('format')!r}")
        self.n_features    = m["n_features"]
        self.feature_order = m["feature_order"]
        self.learning_rate = m["learning_rate"]
        self.init_constant = m["init_constant"]
        self.clamp_lo, self.clamp_hi = m["output_clamp"]
        self.trees         = m["trees"]
        self.raw           = m

    def _tree_value(self, tree: dict, x: list[float]) -> float:
        feature  = tree["feature"]
        thresh   = tree["threshold"]
        left     = tree["children_left"]
        right    = tree["children_right"]
        value    = tree["value"]
        n = 0
        while left[n] != -1:
            n = left[n] if _f32(x[feature[n]]) <= thresh[n] else right[n]
        return value[n]

    def predict_raw(self, x: list[float]) -> float:
        """
        Pre-clamp prediction.

        ACCUMULATION ORDER MATTERS. The formula string in peg_spec.json reads
        "init_constant + learning_rate * sum(tree_value(x) for tree in trees)",
        which literally implies summing all tree values first and scaling once.
        sklearn does NOT do that — its predict_stages accumulates
        ``out += learning_rate * value`` per tree, and so does the miner's own
        reference predict_json(). The two orders differ by ~1.2e-15 in float64
        (measured across 140 real candidates: 127/140 rows deviated).

        That is numerically negligible, but the spec's own equivalence gate is
        max_abs_deviation == 0.0, so the loose wording is followed here by its
        reference implementation, not its prose.
        """
        if len(x) != self.n_features:
            raise ValueError(f"expected {self.n_features} features, got {len(x)}")
        total = self.init_constant
        lr = self.learning_rate
        for tree in self.trees:
            total += lr * self._tree_value(tree, x)
        return total

    def predict(self, x: list[float]) -> float:
        """Clamped prediction — the efficiency value."""
        y = self.predict_raw(x)
        return min(max(y, self.clamp_lo), self.clamp_hi)
