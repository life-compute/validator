"""Shared fixtures.

NOTE: every test here uses a THROWAWAY in-memory key (Signer.generate()).
No real keypair file is read, created, rotated or written by the suite, and
no GPU job is ever launched — the scorer is always mocked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from coordinator_client import hashpin  # noqa: E402
from coordinator_client.rescore import SPCAS9_REC1_200AA  # noqa: E402

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    return json.loads((DATA / "golden_vectors.json").read_text())


@pytest.fixture
def throwaway_signer():
    from coordinator_client.signing import Signer
    return Signer.generate("test-worker")


@pytest.fixture
def msa_dir(tmp_path: Path) -> Path:
    """A fake MSA library with one a3m big enough to pass the 1024-byte floor."""
    d = tmp_path / "msa_files"
    d.mkdir()
    seq = "MKVLAAGIVGLNLGGK" * 20          # 320 aa
    lines = [">query", seq]
    for i in range(80):
        lines += [f">hom{i}", seq]
    (d / "P11802.a3m").write_text("\n".join(lines) + "\n")
    return d


@pytest.fixture
def protein_pins(msa_dir: Path) -> dict[str, str]:
    path = msa_dir / "P11802.a3m"
    seq = hashpin.sequence_from_msa(path)
    assert seq
    return {"pinned_receptor_sha256": hashpin.sha256_text(seq),
            "pinned_msa_sha256": hashpin.sha256_file(path)}


@pytest.fixture
def crispr_pins() -> dict[str, str]:
    return {"pinned_receptor_sha256": hashpin.sha256_text(SPCAS9_REC1_200AA),
            "pinned_msa_sha256": hashpin.SHA256_EMPTY}


SCORER_PROFILE = {"boltz_version": "1.0.1",
                  "flags": ["--use-msa-server", "--recycling-steps=3"],
                  "alignment_sampling": "uniform"}


def pending_row(result_id: int, **over: Any) -> dict[str, Any]:
    """A contract-shaped (extended) pending row."""
    row: dict[str, Any] = {
        "id": result_id,
        "lease_id": "a3f2e8b1c9d04e5f",
        "wallet": "4bXgZ1111111111111111111111111111111111111",
        "molecule_hash": f"mol_{result_id:08x}",
        "target": "CDK4",
        "score": -8.42,
        "submitted_at": 1721000600000,
        "scoring_status": "pending",
        "scoring_attempts": 0,
        "modality": "protein",
        "smiles": "CC(=O)Oc1ccccc1C(=O)O",
        "scorer_profile": dict(SCORER_PROFILE),
        "uniprot_id": "P11802",
        "pinned_receptor_sha256": "0" * 64,
        "pinned_msa_sha256": "0" * 64,
    }
    row.update(over)
    return row


class FakeRunner:
    """Mock Boltz2.  Records every invocation; returns canned metrics.

    This is the ONLY scorer used by the suite — no GPU work ever happens.
    """

    def __init__(self, metrics: dict[str, Any] | list[dict[str, Any]] | None = None,
                 raises: Exception | None = None, delay_s: float = 0.0):
        self.metrics = metrics if metrics is not None else {"affinity_pred_value": 0.0}
        self.raises = raises
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    def run(self, *, yaml_inputs, flags, seed, timeout_s):
        self.calls.append({"yaml": yaml_inputs, "flags": list(flags),
                           "seed": seed, "timeout_s": timeout_s})
        if self.delay_s:
            import time
            time.sleep(self.delay_s)
        if self.raises:
            raise self.raises
        if isinstance(self.metrics, list):
            return self.metrics[min(len(self.calls) - 1, len(self.metrics) - 1)]
        return self.metrics
