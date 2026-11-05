"""Pinned scoring configuration (BATCH_SPEC §17.5).

`GET /scoring_config.json` publishes the tolerance / hit-threshold / sanity
constants the Worker evaluated every verdict with, and the response carries
`X-Config-SHA256`.  §17.5: "Checkers MUST pin this exact SHA-256."

SCORING_CONFIG_SHA256 below is that pin.  It was taken from the file in the
C47 spec bundle (`/root/scratch/c47-spec/scoring_config.json`) and verified
against the `X-Config-SHA256` header the live devnet Worker serves — the two
are equal.  A config whose bytes hash to anything else is refused: a swapped
tolerance table would silently re-decide every verdict.

The lookup is BY VERSION STRING.  Each record carries a `config_version`
string; §17.5 requires the constants for that version to come out of
`version_map`, and an absent version to be `scoring_config_unknown`.
`coord_config_version` is only the currently ACTIVE version — it is never a
fallback for a line that names another one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PayoutConfigError

# §17.5 pin.  Verified equal to the live Worker's X-Config-SHA256 header.
SCORING_CONFIG_SHA256 = (
    "3e807aa47edd21372458e88429bba9ee281bcc9258740d68599fee4e807229a1"
)

# Modality of a target, for picking the tolerance block.  The targets table
# (§12) is the authority; this map is only the fallback used by the golden
# vector, whose single target is ESR1 (protein in targets_table.json).
DEFAULT_MODALITY = "protein"


@dataclass(frozen=True)
class VersionConstants:
    """The constants one `config_version` string resolves to."""
    version: str
    modality_tolerance: dict[str, dict[str, float]]
    target_hit_thresholds: dict[str, float | None]
    sanity_ranges: dict[str, dict[str, float]]

    def tolerance(self, modality: str, bias_active: bool) -> float:
        mt = self.modality_tolerance.get(modality)
        if mt is None:
            raise PayoutConfigError(
                f"scoring_config version {self.version!r} has no "
                f"modality_tolerance entry for {modality!r}")
        key = "tolerance_with_bias" if bias_active else "tolerance_without_bias"
        return float(mt[key])

    def hit_threshold(self, target: str) -> float | None:
        """§17.5: an absent target or a null threshold is ALWAYS a hit."""
        return self.target_hit_thresholds.get(target)


class ScoringConfig:
    """A parsed, SHA-pinned `/scoring_config.json`."""

    def __init__(self, obj: dict[str, Any], sha256: str):
        self.sha256 = sha256
        self.raw = obj
        self.coord_config_version = str(obj.get("coord_config_version") or "")
        vm = obj.get("version_map")
        if not isinstance(vm, dict) or not vm:
            raise PayoutConfigError(
                "scoring_config.json carries no version_map (§17.5)")
        self._versions: dict[str, VersionConstants] = {}
        for version, consts in vm.items():
            if not isinstance(consts, dict):
                raise PayoutConfigError(
                    f"scoring_config version_map[{version!r}] is not an object")
            self._versions[str(version)] = VersionConstants(
                version=str(version),
                modality_tolerance={
                    str(k): {str(kk): float(vv) for kk, vv in v.items()}
                    for k, v in (consts.get("modality_tolerance") or {}).items()},
                target_hit_thresholds={
                    str(k): (None if v is None else float(v))
                    for k, v in (consts.get("target_hit_thresholds") or {}).items()},
                sanity_ranges={
                    str(k): {str(kk): float(vv) for kk, vv in v.items()}
                    for k, v in (consts.get("sanity_ranges") or {}).items()},
            )

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_bytes(cls, raw: bytes,
                   expected_sha256: str = SCORING_CONFIG_SHA256) -> "ScoringConfig":
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha256.lower():
            raise PayoutConfigError(
                f"scoring_config.json SHA-256 {actual} does not match the "
                f"pinned value {expected_sha256} (§17.5); refusing to decide "
                f"any verdict from an unverified scoring configuration")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayoutConfigError(
                f"scoring_config.json is not valid JSON: {exc}") from None
        if not isinstance(obj, dict):
            raise PayoutConfigError("scoring_config.json must be an object")
        return cls(obj, sha256=actual)

    @classmethod
    def from_file(cls, path: str | Path,
                  expected_sha256: str = SCORING_CONFIG_SHA256) -> "ScoringConfig":
        p = Path(path)
        if not p.is_file():
            raise PayoutConfigError(f"scoring_config.json not found: {p}")
        return cls.from_bytes(p.read_bytes(), expected_sha256)

    # ── lookup ──────────────────────────────────────────────────────────────
    def has_version(self, version: str) -> bool:
        return str(version) in self._versions

    def constants(self, version: str) -> VersionConstants:
        try:
            return self._versions[str(version)]
        except KeyError:
            raise KeyError(str(version)) from None

    def versions(self) -> list[str]:
        return sorted(self._versions)
