"""Typed view of the coordinator API contract (/root/api/API_CONTRACT.md, C1b).

Everything the client knows about the wire format lives here, so a contract
change is a one-file edit.

CONTRACT GAP (resolved with the operator, 2026-10-27)
-----------------------------------------------------
API_CONTRACT.md §2.4 defines a pending row as:

    id, lease_id, wallet, molecule_hash, target, score,
    submitted_at, scoring_status, scoring_attempts

That is NOT sufficient to re-score.  Missing: the SMILES to score, the
modality (which selects the tolerance and the scorer path), the lease's
scorer_profile, and the pinned receptor/MSA hashes required to fail closed.
There is also no validator-readable lease lookup endpoint.

Decision: code to an EXTENDED pending row carrying those fields; the
coordinator side will be updated to match.  Until it is, any row missing a
required field is NOT scored — it gets a validator_error with reason code
MISSING_CONTRACT_FIELD.  We never guess an input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────
PATH_PENDING = "/results/pending"
PATH_VERDICTS = "/verdicts"
PATH_CONFIG = "/config"
PATH_HEALTH = "/health"

# ── reason_code values (contract §3.1) ───────────────────────────────────────
REASON_GPU_OOM = "GPU_OOM"
REASON_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
REASON_TIMEOUT = "TIMEOUT"
REASON_INTERNAL_ERROR = "INTERNAL_ERROR"
REASON_UNKNOWN = "UNKNOWN"
# Extensions.  §3.1 states reason_code is "convention, not enforced" and is
# logged free-form to scoring_error_log, so these are contract-legal.
REASON_MISSING_CONTRACT_FIELD = "MISSING_CONTRACT_FIELD"
REASON_MSA_DIR_MISSING = "MSA_DIR_MISSING"
REASON_INPUT_HASH_MISMATCH = "INPUT_HASH_MISMATCH"

CONTRACT_REASON_CODES = frozenset({
    REASON_GPU_OOM, REASON_DOWNLOAD_FAILED, REASON_TIMEOUT,
    REASON_INTERNAL_ERROR, REASON_UNKNOWN,
})

# ── Fallback config, used only until GET /config answers ─────────────────────
# Mirrors API_CONTRACT.md §2.6 verbatim.
DEFAULT_CONFIG: dict[str, Any] = {
    "version": "2.0.0",
    "modality_tolerance": {
        "protein":        {"tolerance_with_bias": 0.25, "tolerance_without_bias": 0.35,
                           "sanity_min": -16.0, "sanity_max": 0.0},
        "mrna":           {"tolerance_with_bias": 0.10, "tolerance_without_bias": 0.10,
                           "sanity_min": -9.5, "sanity_max": -5.5},
        "crispr":         {"tolerance_with_bias": 0.10, "tolerance_without_bias": 0.10,
                           "sanity_min": -9.5, "sanity_max": -5.5},
        "small_molecule": {"tolerance_with_bias": 0.10, "tolerance_without_bias": 0.10,
                           "sanity_min": -16.0, "sanity_max": 0.0},
    },
    "target_hit_thresholds": {},
    "no_threshold_targets": [],
    "scoring": {"rescore_timeout_ms": 1_200_000,
                "reclaim_timeout_ms": 1_500_000,
                "max_attempts": 3},
    "scorer_profile": {"boltz_version": "1.0.1",
                       "flags": ["--use-msa-server", "--recycling-steps=3"],
                       "alignment_sampling": "uniform"},
}

MODALITIES = ("protein", "mrna", "crispr", "small_molecule")

# pegRNA is log-only: scored and recorded, never submitted as a verdict.
LOG_ONLY_MODALITIES = frozenset({"pegrna"})


class ContractError(ValueError):
    """A pending row does not satisfy the contract the client was built to."""


@dataclass(frozen=True)
class ScorerProfile:
    """The lease's scorer profile — the validator uses EXACTLY this."""
    boltz_version: str
    flags: tuple[str, ...]
    alignment_sampling: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScorerProfile":
        if not isinstance(d, dict):
            raise ContractError("scorer_profile is not an object")
        missing = [k for k in ("boltz_version", "flags", "alignment_sampling") if k not in d]
        if missing:
            raise ContractError(f"scorer_profile missing {missing}")
        return cls(str(d["boltz_version"]), tuple(str(f) for f in d["flags"]),
                   str(d["alignment_sampling"]))

    def as_dict(self) -> dict[str, Any]:
        return {"boltz_version": self.boltz_version,
                "flags": list(self.flags),
                "alignment_sampling": self.alignment_sampling}


@dataclass(frozen=True)
class PendingResult:
    """One row from GET /results/pending (extended — see module docstring)."""
    # Contract §2.4 fields
    id: int
    lease_id: str
    wallet: str
    molecule_hash: str
    target: str
    score: float
    submitted_at: int
    scoring_status: str
    scoring_attempts: int
    # Extension fields required for re-scoring
    modality: str
    smiles: str
    scorer_profile: ScorerProfile
    pinned_receptor_sha256: str
    pinned_msa_sha256: str
    # Optional extras
    uniprot_id: str = ""
    gpu_model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingResult":
        """Parse strictly.  Raise ContractError — never invent a value."""
        if not isinstance(d, dict):
            raise ContractError("pending row is not an object")

        def _present(key: str) -> Any:
            if key not in d or d[key] is None:
                raise ContractError(f"missing required field '{key}'")
            return d[key]

        def req_str(key: str) -> str:
            val = _present(key)
            if not isinstance(val, str) or val == "":
                raise ContractError(f"field '{key}' must be a non-empty string, got {val!r}")
            return val

        def req_int(key: str) -> int:
            val = _present(key)
            if not isinstance(val, int) or isinstance(val, bool):
                raise ContractError(f"field '{key}' must be an integer, got {val!r}")
            return val

        def req_float(key: str) -> float:
            val = _present(key)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ContractError(f"field '{key}' must be a number, got {val!r}")
            return float(val)

        modality = req_str("modality").lower()
        if modality not in MODALITIES and modality not in LOG_ONLY_MODALITIES:
            raise ContractError(f"unknown modality '{modality}'")

        return cls(
            id=req_int("id"),
            lease_id=req_str("lease_id"),
            wallet=req_str("wallet"),
            molecule_hash=req_str("molecule_hash"),
            target=req_str("target"),
            score=req_float("score"),
            submitted_at=int(d.get("submitted_at", 0) or 0),
            scoring_status=str(d.get("scoring_status", "pending")),
            scoring_attempts=int(d.get("scoring_attempts", 0) or 0),
            modality=modality,
            smiles=req_str("smiles"),
            scorer_profile=ScorerProfile.from_dict(d.get("scorer_profile") or {}),
            pinned_receptor_sha256=req_str("pinned_receptor_sha256").lower(),
            pinned_msa_sha256=req_str("pinned_msa_sha256").lower(),
            uniprot_id=str(d.get("uniprot_id", "") or ""),
            gpu_model=str(d.get("gpu_model", "") or ""),
            extra={k: v for k, v in d.items() if k not in _KNOWN_FIELDS},
        )


_KNOWN_FIELDS = frozenset({
    "id", "lease_id", "wallet", "molecule_hash", "target", "score",
    "submitted_at", "scoring_status", "scoring_attempts", "modality",
    "smiles", "scorer_profile", "pinned_receptor_sha256",
    "pinned_msa_sha256", "uniprot_id", "gpu_model",
})


def verdict_body(result_id: int, validator_score: float, gpu_model: str | None,
                 validator_verdict: str | None,
                 input_hashes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normal verdict body (contract §2.5 A)."""
    body: dict[str, Any] = {"result_id": int(result_id),
                            "validator_score": float(validator_score)}
    if gpu_model:
        body["gpu_model"] = gpu_model
    if validator_verdict:
        if validator_verdict not in ("confirm", "reject"):
            raise ContractError(f"validator_verdict must be confirm|reject, got {validator_verdict!r}")
        body["validator_verdict"] = validator_verdict
    if input_hashes:
        body["input_hashes"] = input_hashes
    return body


def error_body(result_id: int, reason_code: str) -> dict[str, Any]:
    """Validator-error body (contract §2.5 B)."""
    return {"result_id": int(result_id),
            "status": "validator_error",
            "reason_code": reason_code or REASON_UNKNOWN}


def tolerance_for(cfg: dict[str, Any], modality: str, bias_active: bool) -> float:
    """Coordinator tolerance for *modality*.  This is the ONLY tolerance the
    client knows — the daemon's local VALIDATION_TOLERANCE is gone."""
    mt = (cfg.get("modality_tolerance") or {}).get(modality)
    if not mt:
        raise ContractError(f"no modality_tolerance entry for '{modality}'")
    return float(mt["tolerance_with_bias" if bias_active else "tolerance_without_bias"])


def sanity_range(cfg: dict[str, Any], modality: str) -> tuple[float, float]:
    mt = (cfg.get("modality_tolerance") or {}).get(modality)
    if not mt:
        raise ContractError(f"no modality_tolerance entry for '{modality}'")
    return float(mt["sanity_min"]), float(mt["sanity_max"])


def coord_decision(validator_score: float, adjusted_claimed: float,
                   tolerance: float) -> tuple[str, float]:
    """Reproduce the coordinator's decision (contract §3.2 steps 4-5).

    rel_err = |validator_score - adjusted_claimed| / |adjusted_claimed|
    and, when |adjusted_claimed| == 0, rel_err = |validator_score|.
    """
    denom = abs(adjusted_claimed)
    rel_err = abs(validator_score - adjusted_claimed) / denom if denom else abs(validator_score)
    return ("confirm" if rel_err <= tolerance else "reject"), rel_err
