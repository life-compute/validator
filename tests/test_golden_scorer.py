"""Golden scorer vectors for protein, mRNA and CRISPR.

The expected values come from tests/data/golden_vectors.json, extracted from
STORED validator runs in the pm2 log.  No Boltz / GPU job runs here.

Affinity-path tolerance is ±0.05 as specified.  The CRISPR structure-only
path is checked much more tightly because it is bit-reproducible: 30 repeat
runs at the same seed showed a spread of 0.0.
"""
from __future__ import annotations

import pytest

from coordinator_client.rescore import (
    Rescorer, SGRNA_SCAFFOLD_RNA, SPCAS9_REC1_200AA,
    crispr_affinity, crispr_yaml, mrna_affinity, protein_dg, revcomp,
)
from conftest import FakeRunner, pending_row

AFFINITY_TOL = 0.05


# ── pure formula vectors ─────────────────────────────────────────────────────

def test_protein_golden_vectors(golden):
    vectors = golden["protein"]
    assert len(vectors) >= 20, "golden set too small to be meaningful"
    for v in vectors:
        got = protein_dg(v["pred"])
        assert abs(got - v["expected"]) <= AFFINITY_TOL, (
            f"protein pred={v['pred']}: got {got}, expected {v['expected']}")


def test_mrna_golden_vectors(golden):
    vectors = golden["mrna"]
    assert len(vectors) >= 20
    for v in vectors:
        got = mrna_affinity(v["prob"], v["pred"], v["ha"])
        assert abs(got - v["expected"]) <= AFFINITY_TOL, (
            f"mrna prob={v['prob']} pred={v['pred']} ha={v['ha']}: "
            f"got {got}, expected {v['expected']}")


def test_crispr_golden_vectors(golden):
    vectors = golden["crispr"]
    assert len(vectors) >= 20
    for v in vectors:
        got = crispr_affinity(v["iptm"])
        # The structure-only path is deterministic (30 repeat runs at the same
        # seed gave spread 0.0), so this is far tighter than the ±0.05
        # affinity budget.  The floor is set by the log format, not the model:
        # iptm is printed to 4 dp, and the formula multiplies it by 3, so
        # re-deriving from a logged iptm carries up to 3 * 0.5e-4 = 1.5e-4.
        assert abs(got - v["expected"]) <= 2e-4, (
            f"crispr iptm={v['iptm']}: got {got}, expected {v['expected']}")


def test_protein_formula_is_rt_ln10(golden):
    """Guard the constant itself, not just the outputs."""
    assert protein_dg(6.0) == 0.0
    assert protein_dg(5.0) == pytest.approx(-1.364, abs=1e-3)


def test_crispr_affinity_bounds():
    """iptm in (0,1] keeps the score inside the coordinator sanity window."""
    for iptm in (0.0, 0.25, 0.5, 0.75, 1.0):
        s = crispr_affinity(iptm)
        assert -9.5 <= s <= -5.5


# ── end-to-end through the Rescorer, scorer mocked ──────────────────────────

def test_protein_rescore_end_to_end(golden, msa_dir, protein_pins):
    from coordinator_client.contract import PendingResult

    v = golden["protein"][len(golden["protein"]) // 2]
    runner = FakeRunner({"affinity_pred_value": v["pred"]})
    r = Rescorer(runner, msa_dir=msa_dir, seed=68,
                 num_subsampled_msa=1024, timeout_s=1200)
    item = PendingResult.from_dict(pending_row(1, **protein_pins))

    out = r.rescore(item)
    assert abs(out.score - v["expected"]) <= AFFINITY_TOL
    assert out.input_hashes.receptor_sha256 == protein_pins["pinned_receptor_sha256"]
    assert out.input_hashes.msa_sha256 == protein_pins["pinned_msa_sha256"]
    assert len(out.input_hashes.ligand_smiles_sha256) == 64


def test_crispr_rescore_end_to_end(golden, msa_dir, crispr_pins):
    from coordinator_client.contract import PendingResult

    v = golden["crispr"][0]
    runner = FakeRunner({"iptm": v["iptm"]})
    r = Rescorer(runner, msa_dir=msa_dir, seed=68,
                 num_subsampled_msa=1024, timeout_s=1200)
    item = PendingResult.from_dict(pending_row(
        2, modality="crispr", smiles="ACGTACGTACGTACGTACGT", **crispr_pins))

    out = r.rescore(item)
    assert abs(out.score - v["expected"]) <= 1e-4


def test_mrna_rescore_end_to_end(golden, msa_dir):
    from coordinator_client import hashpin
    from coordinator_client.contract import PendingResult

    rna = "GGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAGCUAG"
    v = golden["mrna"][len(golden["mrna"]) // 2]
    runner = FakeRunner({"affinity_probability_binary": v["prob"],
                         "affinity_pred_value": v["pred"],
                         "heavy_atoms": v["ha"]})
    r = Rescorer(runner, msa_dir=msa_dir, seed=68, num_subsampled_msa=1024,
                 timeout_s=1200, target_sequences={"KRAS_mRNA": rna})
    item = PendingResult.from_dict(pending_row(
        3, modality="mrna", target="KRAS_mRNA",
        pinned_receptor_sha256=hashpin.sha256_text(
            hashpin.mrna_receptor_material(rna)),
        pinned_msa_sha256=hashpin.SHA256_EMPTY))

    out = r.rescore(item)
    assert abs(out.score - v["expected"]) <= AFFINITY_TOL


# ── CRISPR input construction (the 01c6ab2 fix must not regress) ────────────

def test_crispr_chain_b_uses_rna_alphabet():
    y = crispr_yaml("ACGTACGTACGTACGTACGT")
    chain_b = next(s["rna"] for s in y["sequences"] if "rna" in s)
    assert "T" not in chain_b["sequence"], "chain B must be RNA letters (U), not DNA (T)"
    assert chain_b["sequence"].endswith(SGRNA_SCAFFOLD_RNA)
    assert len(chain_b["sequence"]) == 96  # 20 spacer + 76 scaffold


def test_crispr_chain_c_stays_dna_and_has_pam():
    spacer = "ACGTACGTACGTACGTACGT"
    y = crispr_yaml(spacer)
    chain_c = next(s["dna"] for s in y["sequences"] if "dna" in s)
    assert chain_c["sequence"] == revcomp(spacer) + "NGG"
    assert "U" not in chain_c["sequence"]


def test_crispr_has_no_properties_block():
    """Structure-only mode: an affinity block would change the scorer path."""
    assert "properties" not in crispr_yaml("ACGTACGTACGTACGTACGT")


def test_crispr_chain_a_is_spcas9_fragment():
    y = crispr_yaml("ACGTACGTACGTACGTACGT")
    chain_a = next(s["protein"] for s in y["sequences"] if "protein" in s)
    assert chain_a["sequence"] == SPCAS9_REC1_200AA
    assert chain_a["msa"] == "empty"
