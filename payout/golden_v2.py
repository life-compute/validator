"""The golden-vector harness for the C47 v2 checker.

Loads `spec/golden/log_v2/*` exactly as the golden README says a Python
checker must (step "How the Python checker must use these files"), runs the
positive vector and each of the 18 mutation files through
`payout.log_v2.evaluate_day`, and reports the code each produced.

The golden wallets exist on no cluster, so registration is answered from the
manifest's `registered_wallets` list (ManifestRegistry), as verify.ts does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chain_state import ManifestRegistry
from .log_v2 import ACCEPTED, SignedLine, V2Result, evaluate_day
from .scoring_config import ScoringConfig

DEFAULT_GOLDEN_DIR = Path("/root/scratch/c47-spec/spec/golden/log_v2")
DEFAULT_SCORING_CONFIG = Path("/root/scratch/c47-spec/scoring_config.json")

# The golden day's single target.  targets_table.json prices ESR1 as a
# protein target, and verify.ts hard-codes the same modality.
GOLDEN_MODALITY = {"ESR1": "protein", "CDK4": "protein", "MYC": "protein"}


@dataclass
class GoldenCase:
    name: str
    expected: str
    got: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.got == self.expected


class GoldenVectors:
    def __init__(self, golden_dir: str | Path = DEFAULT_GOLDEN_DIR,
                 scoring_config_path: str | Path = DEFAULT_SCORING_CONFIG):
        self.dir = Path(golden_dir)
        self.manifest: dict[str, Any] = json.loads(
            (self.dir / "manifest.json").read_text())
        self.canonical = [json.loads(l) for l in
                          (self.dir / "canonical.ndjson").read_text().splitlines()
                          if l.strip()]
        self.signed_raw = [json.loads(l) for l in
                           (self.dir / "signed.ndjson").read_text().splitlines()
                           if l.strip()]
        self.scoring_config = ScoringConfig.from_file(scoring_config_path)
        self.registry = ManifestRegistry(self.manifest["registered_wallets"])

    # ── expectations from the manifest (verify.ts does the same) ───────────
    def expectations(self) -> dict[tuple[int, str], dict[str, Any]]:
        out: dict[tuple[int, str], dict[str, Any]] = {}
        for r in self.manifest.get("results", ()):
            for v in r.get("validators", ()):
                out[(int(r["result_id"]), str(v["validator_wallet"]))] = {
                    "coord_decision": v.get("coord_decision"),
                    "hit_status": v.get("hit_status"),
                }
        return out

    @staticmethod
    def _modality(target: str) -> str:
        return GOLDEN_MODALITY.get(target, "protein")

    def _run(self, canonical: list[dict[str, Any]],
             signed: list[dict[str, Any]], *,
             canonical_hash: str, signed_hash: str, anchor_v2_memo: str,
             config_version: str | None = None,
             batch_mutation: str | None = None) -> V2Result:
        return evaluate_day(
            canonical_lines=canonical,
            signed_lines=[SignedLine.from_json(s) for s in signed],
            scoring_config=self.scoring_config,
            registry=self.registry,
            expected_canonical_hash=canonical_hash,
            expected_signed_hash=signed_hash,
            anchor_v2_memo=anchor_v2_memo,
            config_version=config_version,
            bias_factor=1.0,
            expectations=self.expectations(),
            modality_of_target=self._modality,
            ledger=None,
            batch_mutation=batch_mutation,
        )

    # ── the positive vector ────────────────────────────────────────────────
    def positive(self) -> V2Result:
        return self._run(self.canonical, self.signed_raw,
                         canonical_hash=self.manifest["canonical_hash"],
                         signed_hash=self.manifest["signed_hash"],
                         anchor_v2_memo=self.manifest["anchor_v2_memo"])

    # ── one mutation file ──────────────────────────────────────────────────
    def mutation(self, path: str | Path) -> V2Result:
        p = Path(path)
        if not p.is_absolute():
            p = self.dir / p
        data = json.loads(p.read_text())
        return self._run(
            data.get("canonical_lines") or self.canonical,
            data.get("signed_lines") or self.signed_raw,
            canonical_hash=data.get("canonical_hash",
                                    self.manifest["canonical_hash"]),
            signed_hash=data.get("signed_hash", self.manifest["signed_hash"]),
            anchor_v2_memo=data.get("anchor_v2_memo",
                                    self.manifest["anchor_v2_memo"]),
            config_version=data.get("_test_override_config_version"),
            batch_mutation=data.get("_test_batch_mutation"),
        )

    def mutation_index(self) -> list[dict[str, str]]:
        return json.loads((self.dir / "negative_mutations.json").read_text())

    # ── everything, as the report table ────────────────────────────────────
    def run_all(self) -> list[GoldenCase]:
        cases: list[GoldenCase] = []
        pos = self.positive()
        cases.append(GoldenCase("POSITIVE", ACCEPTED, pos.code, pos.detail))
        for mut in self.mutation_index():
            res = self.mutation(mut["file"])
            cases.append(GoldenCase(mut["id"], mut["code"], res.code, res.detail))
        return cases


def format_table(cases: list[GoldenCase]) -> str:
    lines = ["| Test | Expected code | Got code | Pass? |",
             "|---|---|---|---|"]
    for c in cases:
        lines.append(f"| {c.name} | {c.expected} | {c.got} | "
                     f"{'PASS' if c.passed else 'FAIL'} |")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual harness
    import sys
    cases = GoldenVectors().run_all()
    print(format_table(cases))
    failed = [c for c in cases if not c.passed]
    for c in failed:
        print(f"  {c.name}: {c.detail}", file=sys.stderr)
    print(f"\n{len(cases) - len(failed)}/{len(cases)} as expected")
    sys.exit(1 if failed else 0)
