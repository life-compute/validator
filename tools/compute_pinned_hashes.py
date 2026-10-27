#!/usr/bin/env python3
"""Compute the coordinator's pinned input hashes for every active target.

The hashes are produced with the validator client's OWN functions
(`coordinator_client.hashpin` + `coordinator_client.rescore`), so what this
writes is bit-identical to what `hashpin.verify_pins()` recomputes from disk
at score time.  Nothing here touches the GPU, PM2, or the running validator.

Per-modality receptor, mirroring coordinator_client.rescore.Rescorer:

  protein (target_type absent)
      receptor = hashpin.sequence_from_msa(<MSA_DIR>/<uniprot>.a3m)
                 i.e. the first non-'#'/'>' line of the alignment
      msa      = hashpin.sha256_file(<same a3m>)
  mrna    (target_type == "mRNA")      -> Rescorer._mrna
      receptor = targets.json "rna_sequence"   (msa_path=None)
  crispr  (target_type == "CRISPR")    -> Rescorer._crispr
      receptor = rescore.SPCAS9_REC1_200AA     (msa_path=None)

No-alignment sentinel
---------------------
`hashpin.verify_pins()` substitutes `hashpin.SHA256_EMPTY`
(= sha256 of the empty string) when `msa_path is None`; it does NOT accept a
literal "none".  A pin of "none" therefore fails the fail-closed gate with
INPUT_HASH_MISMATCH for every mrna/crispr target.  The default below is
`empty` so the emitted file is what the validator actually verifies; pass
`--msa-sentinel none` to emit the literal string instead (it will not verify).

Usage:
    python3 tools/compute_pinned_hashes.py -o /root/scratch/pinned_hashes.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from coordinator_client import hashpin                  # noqa: E402
from coordinator_client.rescore import SPCAS9_REC1_200AA  # noqa: E402

DEFAULT_TARGETS = "/tmp/life-compute/targets/targets.json"
DEFAULT_MSA_DIR = os.environ.get("MSA_DIR", "/mnt/life-data/msa_files")

# targets.json `target_type` -> contract modality.  Absent == protein.
_TARGET_TYPE_TO_MODALITY = {None: "protein", "": "protein",
                            "mRNA": "mrna", "CRISPR": "crispr"}


class TargetError(Exception):
    """A target could not be pinned (missing a3m, missing sequence, ...)."""


def modality_of(target: dict[str, Any]) -> str:
    tt = target.get("target_type")
    try:
        return _TARGET_TYPE_TO_MODALITY[tt]
    except KeyError:
        raise TargetError(f"unknown target_type {tt!r}") from None


def is_active(target: dict[str, Any]) -> bool:
    """targets.json carries no active/status flag today: every row is active.

    Honour the flags anyway if the feed ever grows them, so a retired target
    is not pinned by accident.
    """
    if target.get("active") is False:
        return False
    if str(target.get("status", "active")).lower() in ("retired", "disabled", "inactive"):
        return False
    return True


def pins_for(target: dict[str, Any], msa_dir: Path, msa_sentinel: str,
             ) -> tuple[dict[str, str], str | None]:
    """Return ({...pin fields...}, msa_path_or_None) for one target."""
    modality = modality_of(target)

    if modality == "protein":
        # Exactly Rescorer._protein: the >1024-byte floor lives in msa_path_for.
        msa_path = hashpin.msa_path_for(target.get("uniprot_id", ""), msa_dir)
        if msa_path is None:
            raise TargetError(
                f"no usable a3m for uniprot '{target.get('uniprot_id')}' under {msa_dir}")
        receptor = hashpin.sequence_from_msa(msa_path)
        if not receptor:
            raise TargetError(f"could not read query sequence from {msa_path}")
        msa_sha = hashpin.sha256_file(msa_path)
    else:
        msa_path = None
        if modality == "crispr":
            receptor = SPCAS9_REC1_200AA
        else:  # mrna
            receptor = target.get("rna_sequence") or ""
            if not receptor:
                raise TargetError("mRNA target has no rna_sequence")
        msa_sha = hashpin.SHA256_EMPTY if msa_sentinel == "empty" else "none"

    return ({"name": target.get("protein_name") or target.get("gene_name") or target["id"],
             "modality": modality,
             "pinned_receptor_sha256": hashpin.sha256_text(receptor),
             "pinned_msa_sha256": msa_sha},
            str(msa_path) if msa_path else None)


def verify(target: dict[str, Any], pins: dict[str, str], msa_dir: Path) -> None:
    """Re-run the client's fail-closed gate against the files on disk.

    Raises hashpin.InputPinError exactly as the validator would at score time.
    """
    hashpin.require_msa_dir(msa_dir)
    modality = pins["modality"]
    if modality == "protein":
        msa_path = hashpin.msa_path_for(target.get("uniprot_id", ""), msa_dir)
        if msa_path is None:
            raise hashpin.InputPinError("MSA_DIR_MISSING",
                                        f"no usable a3m for {target['id']}")
        receptor = hashpin.sequence_from_msa(msa_path) or ""
    else:
        msa_path = None
        receptor = (SPCAS9_REC1_200AA if modality == "crispr"
                    else target.get("rna_sequence") or "")
    hashpin.verify_pins(receptor_sequence=receptor, msa_path=msa_path,
                        pinned_receptor_sha256=pins["pinned_receptor_sha256"],
                        pinned_msa_sha256=pins["pinned_msa_sha256"],
                        target=target["id"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", default=DEFAULT_TARGETS)
    ap.add_argument("--msa-dir", default=DEFAULT_MSA_DIR)
    ap.add_argument("-o", "--out", default="/root/scratch/pinned_hashes.json")
    ap.add_argument("--msa-sentinel", choices=("empty", "none"), default="empty",
                    help="pinned_msa_sha256 for targets with no alignment file: "
                         "'empty' = hashpin.SHA256_EMPTY (what verify_pins checks, "
                         "default); 'none' = the literal string (will NOT verify)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the fail-closed re-check")
    args = ap.parse_args(argv)

    msa_dir = Path(args.msa_dir)
    hashpin.require_msa_dir(msa_dir)          # fail closed before any work
    targets = json.loads(Path(args.targets).read_text())

    out: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for t in targets:
        if not is_active(t):
            continue
        try:
            pins, _ = pins_for(t, msa_dir, args.msa_sentinel)
        except TargetError as e:
            errors.append(f"{t.get('id')}: {e}")
            continue
        out[t["id"]] = pins

    Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    failures: list[str] = []
    if not args.no_verify:
        by_id = {t["id"]: t for t in targets}
        for tid, pins in out.items():
            try:
                verify(by_id[tid], pins, msa_dir)
            except hashpin.InputPinError as e:
                failures.append(f"{tid}: [{e.reason_code}] {e.detail}")

    n_mod: dict[str, int] = {}
    for p in out.values():
        n_mod[p["modality"]] = n_mod.get(p["modality"], 0) + 1
    print(f"wrote {args.out}: {len(out)} targets {n_mod}")
    for e in errors:
        print(f"  [PIN-ERROR] {e}")
    if args.no_verify:
        print("  verification skipped (--no-verify)")
    elif failures:
        print(f"  FAIL-CLOSED CHECK: {len(failures)}/{len(out)} targets REJECTED")
        for f in failures:
            print(f"    [VERIFY-FAIL] {f}")
    else:
        print(f"  FAIL-CLOSED CHECK: all {len(out)}/{len(out)} targets PASS "
              f"(hashpin.verify_pins against files on disk)")
    return 1 if (errors or failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
