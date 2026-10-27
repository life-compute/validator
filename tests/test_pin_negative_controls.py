"""Negative controls for the input-hash pins — one per modality.

Every test here flips exactly ONE byte of ONE input on a TEMPORARY COPY
(tmp_path / an in-memory string) and asserts the validator's fail-closed gate
raises InputPinError with reason_code == "INPUT_HASH_MISMATCH".  Nothing
touches /mnt/life-data, /tmp/life-compute, the GPU, or PM2.

Plus the "pinned to the empty hash while the input exists" control, which is
the failure mode that lets a target be scored against nothing.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from coordinator_client import hashpin                      # noqa: E402
from coordinator_client.rescore import (                    # noqa: E402
    SGRNA_SCAFFOLD_RNA, SPCAS9_REC1_200AA,
)
import compute_pinned_hashes as cph                         # noqa: E402


A3M_BODY = (
    "#123\t1\n"
    ">query\n"
    "MEEPQSDPSVEPPLSQETFSDLWKLLPENNVLSPLPSQAMDDLMLSPDDIEQWFTEDPGPDEAPRM\n"
    ">sp|P00001|HOMOLOG_A\n"
    "MEEPQSDPSVEPPLSQETFSDLWKLLPENNVLSPLPSQAMDDLMLSPDDIEQWFTEDPGPDEAPRM\n"
    ">sp|P00002|HOMOLOG_B\n"
    "MEDPQSDPSVEPPLSQETFSDLWKLLPENNVLSPLPSQAMDDLMLSPDDIEQWFTEDPGPDEAPRL\n"
) + ">filler\n" + ("ACDEFGHIKLMNPQRSTVWY" * 60 + "\n")   # keep it >1024 bytes


def flip_one_byte(path: Path, offset: int) -> None:
    """Change exactly one byte in place, preserving length."""
    data = bytearray(path.read_bytes())
    old = data[offset]
    data[offset] = ord("Q") if old != ord("Q") else ord("H")
    assert data[offset] != old
    path.write_bytes(bytes(data))


@pytest.fixture
def msa_dir(tmp_path: Path) -> Path:
    """A throwaway MSA library — never the real /mnt/life-data one."""
    d = tmp_path / "msa_files"
    d.mkdir()
    (d / "P04637.a3m").write_text(A3M_BODY)
    assert (d / "P04637.a3m").stat().st_size > 1024
    return d


PROTEIN_T = {"id": "TP53", "uniprot_id": "P04637", "protein_name": "p53"}
MRNA_T = {"id": "MYC_mRNA", "target_type": "mRNA", "protein_name": "MYC mRNA",
          "rna_sequence": "AUGCCCAGCGAGGAGAGCUUCACCCCCACCGCCCAGCUCCCGCAACAGCGGCGGCAGCAGCC"}
CRISPR_T = {"id": "TP53_CRISPR", "target_type": "CRISPR", "gene_name": "TP53",
            "uniprot_id": "P04637", "protein_name": "TP53 gRNA target"}


# ── protein: one byte of the alignment file ──────────────────────────────────

def test_protein_msa_byte_flip_is_rejected(msa_dir: Path, tmp_path: Path):
    pins, msa_path = cph.pins_for(PROTEIN_T, msa_dir, "empty")
    cph.verify(PROTEIN_T, pins, msa_dir)                     # baseline passes

    # Temporary copy of the whole library; the original is untouched.
    scratch = tmp_path / "scratch_msa"
    shutil.copytree(msa_dir, scratch)
    victim = scratch / "P04637.a3m"
    before = victim.read_bytes()
    # Byte inside a HOMOLOG line: the query sequence (and so the receptor
    # hash) is unchanged — only the alignment hash moves.
    flip_one_byte(victim, before.index(b"HOMOLOG_B") + 40)
    assert len(victim.read_bytes()) == len(before)
    assert (msa_dir / "P04637.a3m").read_bytes() == before   # real file intact

    assert hashpin.sequence_from_msa(victim) == hashpin.sequence_from_msa(msa_path)
    with pytest.raises(hashpin.InputPinError) as ei:
        cph.verify(PROTEIN_T, pins, scratch)
    assert ei.value.reason_code == "INPUT_HASH_MISMATCH"
    assert "MSA sha256" in ei.value.detail


def test_protein_receptor_byte_flip_is_rejected(msa_dir: Path, tmp_path: Path):
    pins, _ = cph.pins_for(PROTEIN_T, msa_dir, "empty")
    scratch = tmp_path / "scratch_msa2"
    shutil.copytree(msa_dir, scratch)
    victim = scratch / "P04637.a3m"
    data = victim.read_bytes()
    flip_one_byte(victim, data.index(b"MEEPQSDPSV") + 3)     # inside the query line
    with pytest.raises(hashpin.InputPinError) as ei:
        cph.verify(PROTEIN_T, pins, scratch)
    assert ei.value.reason_code == "INPUT_HASH_MISMATCH"
    assert "receptor sequence sha256" in ei.value.detail


# ── mRNA: one byte of the target RNA sequence ────────────────────────────────

def test_mrna_rna_sequence_byte_flip_is_rejected(msa_dir: Path):
    pins, _ = cph.pins_for(MRNA_T, msa_dir, "empty")
    cph.verify(MRNA_T, pins, msa_dir)

    mutated = dict(MRNA_T)                                   # copy, not the feed
    seq = MRNA_T["rna_sequence"]
    mutated["rna_sequence"] = seq[:7] + ("G" if seq[7] != "G" else "A") + seq[8:]
    assert len(mutated["rna_sequence"]) == len(seq)

    with pytest.raises(hashpin.InputPinError) as ei:
        cph.verify(mutated, pins, msa_dir)
    assert ei.value.reason_code == "INPUT_HASH_MISMATCH"
    assert "receptor sequence sha256" in ei.value.detail


# ── CRISPR: one byte of each pinned constant ─────────────────────────────────

@pytest.mark.parametrize("which", ["cas9", "scaffold"])
def test_crispr_constant_byte_flip_is_rejected(msa_dir: Path, which: str):
    pins, _ = cph.pins_for(CRISPR_T, msa_dir, "empty")
    cph.verify(CRISPR_T, pins, msa_dir)

    cas9, scaf = SPCAS9_REC1_200AA, SGRNA_SCAFFOLD_RNA
    if which == "cas9":
        cas9 = cas9[:5] + ("A" if cas9[5] != "A" else "G") + cas9[6:]
        assert len(cas9) == len(SPCAS9_REC1_200AA)
    else:
        # exactly commit 01c6ab2's bug class: one RNA letter back to DNA
        scaf = scaf[:3] + "T" + scaf[4:]
        assert len(scaf) == len(SGRNA_SCAFFOLD_RNA)

    with pytest.raises(hashpin.InputPinError) as ei:
        hashpin.verify_pins(
            receptor_sequence=hashpin.crispr_receptor_material(cas9, scaf),
            msa_path=None,
            pinned_receptor_sha256=pins["pinned_receptor_sha256"],
            pinned_msa_sha256=pins["pinned_msa_sha256"],
            target=CRISPR_T["id"])
    assert ei.value.reason_code == "INPUT_HASH_MISMATCH"


# ── empty pin while the input exists ─────────────────────────────────────────

def test_empty_msa_pin_with_a_real_alignment_is_rejected(msa_dir: Path):
    """A protein pinned to SHA256_EMPTY while its a3m exists must NOT score."""
    pins, msa_path = cph.pins_for(PROTEIN_T, msa_dir, "empty")
    assert msa_path is not None and msa_path.exists()
    broken = dict(pins, pinned_msa_sha256=hashpin.SHA256_EMPTY)

    with pytest.raises(hashpin.InputPinError) as ei:
        cph.verify(PROTEIN_T, broken, msa_dir)
    assert ei.value.reason_code == "INPUT_HASH_MISMATCH"
    assert "EMPTY hash but the alignment file" in ei.value.detail


def test_empty_receptor_pin_with_a_real_receptor_is_rejected(msa_dir: Path):
    pins, _ = cph.pins_for(MRNA_T, msa_dir, "empty")
    broken = dict(pins, pinned_receptor_sha256=hashpin.SHA256_EMPTY)
    with pytest.raises(hashpin.InputPinError) as ei:
        cph.verify(MRNA_T, broken, msa_dir)
    assert ei.value.reason_code == "INPUT_HASH_MISMATCH"
    assert "EMPTY hash but a real receptor input exists" in ei.value.detail


# ── positive controls: the emitted file is complete and self-consistent ──────

def test_every_emitted_pin_is_real_or_has_a_reason(msa_dir: Path, tmp_path: Path):
    feed = [PROTEIN_T, MRNA_T, CRISPR_T]
    tf = tmp_path / "targets.json"
    tf.write_text(json.dumps(feed))
    out = tmp_path / "pins.json"
    rc = cph.main(["--targets", str(tf), "--msa-dir", str(msa_dir),
                   "-o", str(out)])
    assert rc == 0
    pins = json.loads(out.read_text())
    assert set(pins) == {"TP53", "MYC_mRNA", "TP53_CRISPR"}
    for tid, p in pins.items():
        assert p["pinned_receptor_sha256"] != hashpin.SHA256_EMPTY, tid
        assert p["pinned_receptor_sha256"], tid
        if p["pinned_msa_sha256"] in ("", hashpin.SHA256_EMPTY):
            assert p["msa_empty_reason"], tid
        assert p["scoreable"] is True, tid
    assert pins["TP53"]["pinned_msa_sha256"] != hashpin.SHA256_EMPTY


def test_unresolvable_protein_is_emitted_with_a_reason_not_dropped(
        msa_dir: Path, tmp_path: Path):
    feed = [dict(PROTEIN_T, id="GHOST", uniprot_id="Q00000")]
    tf = tmp_path / "targets.json"
    tf.write_text(json.dumps(feed))
    out = tmp_path / "pins.json"
    rc = cph.main(["--targets", str(tf), "--msa-dir", str(msa_dir),
                   "-o", str(out)])
    assert rc == 1                                   # loud, not silent
    p = json.loads(out.read_text())["GHOST"]
    assert p["scoreable"] is False
    assert "no usable a3m" in p["msa_empty_reason"]
    assert p["pinned_msa_sha256"] == hashpin.SHA256_EMPTY


def test_modalities_cannot_collide(msa_dir: Path):
    """Domain separation: same letters, different modality -> different pin."""
    seq = "ACGUACGUACGU"
    assert (hashpin.sha256_text(hashpin.mrna_receptor_material(seq))
            != hashpin.sha256_text(seq))
    assert (hashpin.crispr_receptor_material(SPCAS9_REC1_200AA, SGRNA_SCAFFOLD_RNA)
            != SPCAS9_REC1_200AA)
