"""Re-scoring: the existing Boltz2 paths, behind a mockable seam.

The three live scoring formulas are reproduced here EXACTLY as the running
validator computes them (validator_daemon.py @ 01c6ab2):

  protein  ΔG = round(RT*ln10 * (affinity_pred_value - 6.0), 3)
           with RT*ln10 = 1.364247 kcal/mol.  affinity_pred_value is
           log10(IC50 / 1 µM), so ΔG = RT·ln(IC50_molar).
  mrna     round(-((prob - pred) / heavy_atoms) * 30.0, 3)
  crispr   round(-6.0 - 3.0 * iptm, 4)   (structure-only 3-chain complex)

pegRNA is LOG-ONLY: it is scored and recorded but never turned into a
verdict (contract has no pegrna modality).

The Boltz invocation sits behind the `BoltzRunner` protocol so tests can run
the whole pipeline with a mocked scorer and never touch the GPU.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import hashpin
from .contract import PendingResult

log = logging.getLogger("life-validator.rescore")

# RT·ln(10) at 298.15 K, kcal/mol.  Copied verbatim from the miner's
# _RT_LN10_KCAL — keep the two in lockstep.
RT_LN10_KCAL = 1.364247

# SpCas9 recognition-lobe fragment used for every CRISPR target (chain A).
SPCAS9_REC1_200AA = (
    "MDKKYSIGLDIGTNSVGWAVITDEYKVPSKKFKVLGNTDRHSIKKNLIGALLFDSGETAEATRLKRTARRRYTRRK"
    "NRICYLQEIFSNEMAKVDDSFFHRLEESFLVEEDKKHERHPIFGNIVDEVAYHEKYPTIYHLRKKLVDSTDKADLRL"
    "IYLALAHMIKFRGHFLIEGDLNPDNSDVDKLFIQLVQTYNQLFEENP"
)

# Canonical sgRNA scaffold in RNA letters (U, not T).  Chain B is declared
# `rna:` and Boltz2 does NOT coerce the alphabet — DNA letters here depress
# iptm and bias every rescore.  See commit 01c6ab2.
SGRNA_SCAFFOLD_RNA = (
    "GUUUUAGAGCUAGAAAUAGCAAGUUAAAAUAAGGCUAGUCCGUUAUCAACUUGAAAAAGUGGCACCGAGUCGGUGC"
)


class RescoreTimeout(Exception):
    """The re-score exceeded the contract's rescore_timeout_ms."""


class RescoreFailed(Exception):
    def __init__(self, reason_code: str, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


class BoltzRunner(Protocol):
    """Seam over the Boltz2 invocation.

    Returns the merged affinity_*/confidence_* metric dict, exactly like
    validator_daemon._read_boltz_affinity does.
    """

    def run(self, *, yaml_inputs: dict[str, Any], flags: list[str],
            seed: int, timeout_s: float) -> dict[str, Any]:
        ...


# ── pure formulas (no I/O, directly unit-testable) ───────────────────────────

def protein_dg(affinity_pred_value: float) -> float:
    return round(RT_LN10_KCAL * (float(affinity_pred_value) - 6.0), 3)


def mrna_affinity(prob: float, pred: float, heavy_atoms: int) -> float:
    if not heavy_atoms:
        raise RescoreFailed("INTERNAL_ERROR", "heavy atom count is zero")
    return round(-((float(prob) - float(pred)) / heavy_atoms) * 30.0, 3)


def crispr_affinity(iptm: float) -> float:
    return round(-6.0 - 3.0 * float(iptm), 4)


# ── Boltz input construction ─────────────────────────────────────────────────

def protein_yaml(sequence: str, smiles: str, msa_path: str) -> dict[str, Any]:
    return {"version": 1,
            "sequences": [{"protein": {"id": "A", "sequence": sequence, "msa": msa_path}},
                          {"ligand": {"id": "B", "smiles": smiles}}],
            "properties": [{"affinity": {"binder": "B"}}]}


def mrna_yaml(rna_sequence: str, smiles: str) -> dict[str, Any]:
    return {"version": 1,
            "sequences": [{"rna": {"id": "A", "sequence": rna_sequence}},
                          {"ligand": {"id": "B", "smiles": smiles}}],
            "properties": [{"affinity": {"binder": "B"}}]}


_DNA_COMP = {"A": "T", "T": "A", "G": "C", "C": "G"}


def revcomp(seq: str) -> str:
    return "".join(_DNA_COMP.get(b, "N") for b in reversed(seq.upper()))


def crispr_yaml(grna_seq: str) -> dict[str, Any]:
    """3-chain ternary complex; no properties block => structure-only mode."""
    spacer = grna_seq.upper()
    return {"version": 1,
            "sequences": [
                {"protein": {"id": "A", "sequence": SPCAS9_REC1_200AA, "msa": "empty"}},
                {"rna": {"id": "B", "sequence": spacer.replace("T", "U") + SGRNA_SCAFFOLD_RNA}},
                {"dna": {"id": "C", "sequence": revcomp(spacer) + "NGG"}},
            ]}


def boltz_flags_for(profile, modality: str, has_msa: bool,
                    num_subsampled_msa: int) -> list[str]:
    """Flags for one re-score.

    The lease's scorer_profile flags are used EXACTLY as given — that is the
    contract's guarantee that miner and validator ran the same scorer.  The
    MSA depth cap is appended only when a real alignment is attached and the
    profile has not already specified its own subsampling.
    """
    flags = list(profile.flags)
    if modality == "protein" and has_msa:
        already = any(f.startswith(("--subsample_msa", "--num_subsampled_msa",
                                    "--max_msa_seqs")) for f in flags)
        if not already:
            flags += ["--subsample_msa",
                      f"--num_subsampled_msa={num_subsampled_msa}",
                      f"--max_msa_seqs={num_subsampled_msa}"]
    return flags


# ── the scored outcome ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class RescoreOutcome:
    score: float
    input_hashes: hashpin.InputHashRecord
    elapsed_s: float
    log_only: bool = False


class Rescorer:
    """Runs the right Boltz2 path for a pending result, fail-closed."""

    def __init__(self, runner: BoltzRunner, *, msa_dir: Path, seed: int,
                 num_subsampled_msa: int, timeout_s: float,
                 target_sequences: dict[str, str] | None = None):
        self.runner = runner
        self.msa_dir = Path(msa_dir)
        self.seed = seed
        self.num_subsampled_msa = num_subsampled_msa
        self.timeout_s = timeout_s
        # target -> receptor/RNA sequence, for non-protein modalities whose
        # receptor does not come from an a3m.
        self.target_sequences = target_sequences or {}

    # ── entry point ─────────────────────────────────────────────────────────
    def rescore(self, item: PendingResult) -> RescoreOutcome:
        started = time.monotonic()

        # Gate 1 — MSA library must be mounted, for every modality.  If it is
        # gone the host is misconfigured and no score is trustworthy.
        hashpin.require_msa_dir(self.msa_dir)

        if item.modality == "protein":
            outcome = self._protein(item)
        elif item.modality in ("mrna", "small_molecule"):
            outcome = self._mrna(item)
        elif item.modality == "crispr":
            outcome = self._crispr(item)
        elif item.modality in ("pegrna",):
            outcome = self._crispr(item)
            outcome = RescoreOutcome(outcome.score, outcome.input_hashes,
                                     outcome.elapsed_s, log_only=True)
        else:
            raise RescoreFailed("INTERNAL_ERROR", f"no scorer for modality {item.modality}")

        elapsed = time.monotonic() - started
        if elapsed > self.timeout_s:
            raise RescoreTimeout(f"re-score took {elapsed:.0f}s > {self.timeout_s:.0f}s")
        return RescoreOutcome(outcome.score, outcome.input_hashes, elapsed,
                              outcome.log_only)

    # ── per-modality paths ──────────────────────────────────────────────────
    def _protein(self, item: PendingResult) -> RescoreOutcome:
        msa_path = hashpin.msa_path_for(item.uniprot_id, self.msa_dir)
        if msa_path is None:
            # No usable alignment.  Falling back to single-sequence mode would
            # produce a score that is not comparable to the miner's, so this
            # is a skip, not a degraded score.
            raise hashpin.InputPinError(
                "MSA_DIR_MISSING",
                f"no usable a3m for uniprot '{item.uniprot_id}' under "
                f"{self.msa_dir} — refusing single-sequence fallback")

        sequence = hashpin.sequence_from_msa(msa_path)
        if not sequence:
            raise hashpin.InputPinError(
                "INPUT_HASH_MISMATCH",
                f"could not read query sequence from {msa_path}")

        receptor_sha, msa_sha = hashpin.verify_pins(
            receptor_sequence=sequence, msa_path=msa_path,
            pinned_receptor_sha256=item.pinned_receptor_sha256,
            pinned_msa_sha256=item.pinned_msa_sha256, target=item.target)

        flags = boltz_flags_for(item.scorer_profile, "protein", True,
                                self.num_subsampled_msa)
        metrics = self._run(protein_yaml(sequence, item.smiles, str(msa_path)), flags)

        pred = metrics.get("affinity_pred_value")
        if pred is None:
            raise RescoreFailed("INTERNAL_ERROR",
                                f"protein rescore produced no affinity_pred_value "
                                f"(keys={sorted(metrics)})")
        score = protein_dg(pred)
        log.info("  [PROTEIN-SCORE] ΔG=RT*ln10*(pred-6)  pred=%.4f → %.3f kcal/mol",
                 float(pred), score)
        return RescoreOutcome(score,
                              hashpin.build_record(receptor_sha, msa_sha, item.smiles, flags),
                              0.0)

    def _mrna(self, item: PendingResult) -> RescoreOutcome:
        sequence = self.target_sequences.get(item.target, "")
        if not sequence:
            raise RescoreFailed("INTERNAL_ERROR",
                                f"no RNA sequence known for target {item.target}")
        receptor_sha, msa_sha = hashpin.verify_pins(
            receptor_sequence=hashpin.mrna_receptor_material(sequence),
            msa_path=None,
            pinned_receptor_sha256=item.pinned_receptor_sha256,
            pinned_msa_sha256=item.pinned_msa_sha256, target=item.target)

        flags = boltz_flags_for(item.scorer_profile, item.modality, False,
                                self.num_subsampled_msa)
        metrics = self._run(mrna_yaml(sequence, item.smiles), flags)

        prob = metrics.get("affinity_probability_binary")
        pred = metrics.get("affinity_pred_value")
        ha = metrics.get("heavy_atoms")
        if prob is None or pred is None or not ha:
            raise RescoreFailed("INTERNAL_ERROR",
                                f"mRNA rescore missing affinity fields "
                                f"(keys={sorted(metrics)})")
        score = mrna_affinity(prob, pred, int(ha))
        log.info("  [mRNA-SCORE-SOURCE] affinity path  prob=%.4f pred=%.4f ha=%d → %.3f kcal/mol",
                 float(prob), float(pred), int(ha), score)
        return RescoreOutcome(score,
                              hashpin.build_record(receptor_sha, msa_sha, item.smiles, flags),
                              0.0)

    def _crispr(self, item: PendingResult) -> RescoreOutcome:
        grna = item.smiles.strip().upper()
        if not grna:
            raise RescoreFailed("INTERNAL_ERROR", "empty gRNA sequence")

        # Chain A is a fixed SpCas9 fragment with msa="empty" and chain B is
        # <spacer> + a fixed sgRNA scaffold: the receptor the coordinator pins
        # for a CRISPR target is BOTH constants, so editing either one (as
        # commit 01c6ab2 did to the scaffold) trips the fail-closed gate
        # instead of silently shifting every iptm.
        receptor_sha, msa_sha = hashpin.verify_pins(
            receptor_sequence=hashpin.crispr_receptor_material(
                SPCAS9_REC1_200AA, SGRNA_SCAFFOLD_RNA),
            msa_path=None,
            pinned_receptor_sha256=item.pinned_receptor_sha256,
            pinned_msa_sha256=item.pinned_msa_sha256, target=item.target)

        flags = boltz_flags_for(item.scorer_profile, "crispr", False,
                                self.num_subsampled_msa)
        metrics = self._run(crispr_yaml(grna), flags)

        iptm = metrics.get("iptm")
        if iptm is None:
            raise RescoreFailed("INTERNAL_ERROR",
                                f"CRISPR rescore produced no iptm (keys={sorted(metrics)})")
        score = crispr_affinity(iptm)
        log.info("  [CRISPR-BOLTZ] -6.0-3.0*iptm  gRNA=%s…  iptm=%.4f → %.4f kcal/mol",
                 grna[:16], float(iptm), score)
        return RescoreOutcome(score,
                              hashpin.build_record(receptor_sha, msa_sha, item.smiles, flags),
                              0.0)

    # ── runner plumbing ─────────────────────────────────────────────────────
    def _run(self, yaml_inputs: dict[str, Any], flags: list[str]) -> dict[str, Any]:
        try:
            metrics = self.runner.run(yaml_inputs=yaml_inputs, flags=flags,
                                      seed=self.seed, timeout_s=self.timeout_s)
        except RescoreTimeout:
            raise
        except MemoryError as e:
            raise RescoreFailed("GPU_OOM", str(e)) from e
        except Exception as e:
            msg = str(e)
            if "out of memory" in msg.lower() or "oom" in msg.lower():
                raise RescoreFailed("GPU_OOM", msg) from e
            raise RescoreFailed("INTERNAL_ERROR", msg) from e
        if not isinstance(metrics, dict) or not metrics:
            raise RescoreFailed("INTERNAL_ERROR", "scorer returned no metrics")
        return metrics
