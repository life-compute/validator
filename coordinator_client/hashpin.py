"""Input-hash pinning — the fail-closed gate before any scoring happens.

Rules (all must hold, else the target is NOT scored):

  1. The MSA directory must exist and be a directory.
  2. For protein targets, the receptor sequence actually fed to Boltz must
     hash to the coordinator's pinned receptor sha256.
  3. The MSA file actually fed to Boltz must hash to the pinned MSA sha256.

A failure here is logged and the target is skipped with a validator_error —
we never score with inputs that differ from what the coordinator pinned, and
we never silently substitute a fallback sequence.

The hash record accompanying every verdict is built here too:
receptor sha256, MSA sha256, ligand SMILES sha256, and the Boltz flags.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("life-validator.hashpin")

# Sentinel used by the Boltz YAML when no alignment is attached.
EMPTY_MSA = "empty"
# sha256 of the empty string — the pinned value a modality with no MSA uses.
SHA256_EMPTY = hashlib.sha256(b"").hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class InputPinError(Exception):
    """Pinned-hash / MSA-availability check failed — do not score."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class InputHashRecord:
    """Exactly what gets attached to the verdict."""
    receptor_sha256: str
    msa_sha256: str
    ligand_smiles_sha256: str
    boltz_flags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"receptor_sha256": self.receptor_sha256,
                "msa_sha256": self.msa_sha256,
                "ligand_smiles_sha256": self.ligand_smiles_sha256,
                "boltz_flags": list(self.boltz_flags)}


def msa_dir_ok(msa_dir: str | Path) -> bool:
    return Path(msa_dir).is_dir()


def require_msa_dir(msa_dir: str | Path) -> None:
    """Fail closed when the MSA library is not mounted."""
    if not msa_dir_ok(msa_dir):
        raise InputPinError(
            "MSA_DIR_MISSING",
            f"MSA directory {msa_dir} is missing or not a directory — "
            f"refusing to score (a single-sequence fallback would not be "
            f"comparable to the miner's score)",
        )


def msa_path_for(uniprot_id: str, msa_dir: str | Path) -> Path | None:
    """Return the a3m path for *uniprot_id*, or None when unusable.

    The >1024-byte floor mirrors the miner's `_msa_path_for`: a truncated or
    failed download must not be treated as a usable alignment.
    """
    if not uniprot_id:
        return None
    p = Path(msa_dir) / f"{uniprot_id}.a3m"
    if p.exists() and p.stat().st_size > 1024:
        return p
    return None


def sequence_from_msa(msa_path: str | Path) -> str | None:
    """First non-comment, non-header line of an a3m = the query sequence."""
    try:
        with open(msa_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith(("#", ">")):
                    return line
    except OSError:
        return None
    return None


def verify_pins(*, receptor_sequence: str, msa_path: Path | None,
                pinned_receptor_sha256: str, pinned_msa_sha256: str,
                target: str) -> tuple[str, str]:
    """Check the real inputs against the coordinator's pinned hashes.

    Returns (receptor_sha256, msa_sha256) on success.
    Raises InputPinError on any mismatch — caller skips the target.
    """
    receptor_sha = sha256_text(receptor_sequence)
    if receptor_sha != pinned_receptor_sha256:
        raise InputPinError(
            "INPUT_HASH_MISMATCH",
            f"target {target}: receptor sequence sha256 {receptor_sha[:16]}… != "
            f"pinned {pinned_receptor_sha256[:16]}… "
            f"(len={len(receptor_sequence)}) — skipping",
        )

    if msa_path is None:
        msa_sha = SHA256_EMPTY
    else:
        msa_sha = sha256_file(msa_path)
    if msa_sha != pinned_msa_sha256:
        where = str(msa_path) if msa_path else "<no alignment>"
        raise InputPinError(
            "INPUT_HASH_MISMATCH",
            f"target {target}: MSA sha256 {msa_sha[:16]}… != pinned "
            f"{pinned_msa_sha256[:16]}… (file {where}) — skipping",
        )

    return receptor_sha, msa_sha


def build_record(receptor_sha256: str, msa_sha256: str, smiles: str,
                 boltz_flags) -> InputHashRecord:
    return InputHashRecord(
        receptor_sha256=receptor_sha256,
        msa_sha256=msa_sha256,
        ligand_smiles_sha256=sha256_text(smiles),
        boltz_flags=tuple(boltz_flags),
    )
