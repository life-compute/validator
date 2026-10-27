#!/usr/bin/env python3
"""Compute the coordinator's pinned input hashes for every active target.

The hashes are produced with the validator client's OWN functions
(`coordinator_client.hashpin` + `coordinator_client.rescore`), so what this
writes is bit-identical to what `hashpin.verify_pins()` recomputes from disk
at score time.  Nothing here touches the GPU, PM2, or the running validator.

What each pin covers, per modality (mirrors coordinator_client.rescore.Rescorer):

  protein (target_type absent)          -> Rescorer._protein
      receptor = hashpin.sequence_from_msa(<MSA_DIR>/<uniprot>.a3m)
                 i.e. the literal chain-A sequence Boltz is handed
      msa      = hashpin.sha256_file(<same a3m>)   <- a REAL file hash
  mrna    (target_type == "mRNA")       -> Rescorer._mrna
      receptor = hashpin.mrna_receptor_material(targets.json "rna_sequence")
                 i.e. the chain-A RNA sequence, domain-separated
      msa      = SHA256_EMPTY + reason (an RNA chain gets no alignment)
  crispr  (target_type == "CRISPR")     -> Rescorer._crispr
      receptor = hashpin.crispr_receptor_material(SPCAS9_REC1_200AA,
                                                  SGRNA_SCAFFOLD_RNA)
                 i.e. BOTH fixed constants: the chain-A Cas9 REC1 fragment and
                 the chain-B sgRNA scaffold.  Editing either changes the pin.
      msa      = SHA256_EMPTY + reason (chain A is run with msa="empty")

The miner-supplied part of a CRISPR/mRNA job (the gRNA spacer, the ligand
SMILES) is not coordinator-side and is covered by ligand_smiles_sha256 on the
verdict, not by these pins.

Empty hashes
------------
`hashpin.verify_pins()` substitutes `hashpin.SHA256_EMPTY` (sha256 of the empty
string) when `msa_path is None`; it does NOT accept a literal "none".  A pin of
"none" therefore fails the fail-closed gate with INPUT_HASH_MISMATCH for every
mrna/crispr target.  The default below is `empty` so the emitted file is what
the validator actually verifies; `--msa-sentinel none` emits the literal string
(it will not verify) and is kept only for diffing against older runs.

Every empty hash carries a `*_empty_reason` string next to it, and a target is
only allowed an empty hash when the modality genuinely has no such input
(hashpin.NO_ALIGNMENT_REASON).  A protein whose a3m cannot be resolved is NOT
silently dropped: it is emitted with empty hashes, a reason, scoreable=false,
and the script exits non-zero.

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
from coordinator_client.rescore import (                # noqa: E402
    SGRNA_SCAFFOLD_RNA, SPCAS9_REC1_200AA,
)

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


def resolve_msa(target: dict[str, Any], msa_dir: Path) -> tuple[Path | None, str]:
    """Resolve a protein target's a3m, reporting HOW it was resolved.

    `hashpin.msa_path_for` is the authority (it carries the >1024-byte floor
    the miner uses).  It is tried on the uniprot id first — that is exactly
    what Rescorer._protein does at score time, so only a candidate that also
    satisfies it may be returned.  The extra candidates below exist so that a
    cosmetic id difference (lowercase, an isoform suffix like 'P04637-2', or a
    gene-name-keyed file) is reported as a rename rather than silently
    becoming a target with no alignment hash.
    """
    uniprot = (target.get("uniprot_id") or "").strip()
    primary = hashpin.msa_path_for(uniprot, msa_dir)
    if primary is not None:
        return primary, "uniprot_id"

    tried = [uniprot]
    candidates: list[tuple[str, str]] = []
    if uniprot:
        candidates.append((uniprot.upper(), "uniprot_id.upper"))
        if "-" in uniprot:
            candidates.append((uniprot.split("-", 1)[0], "uniprot_id sans isoform"))
    for key in ("gene_name", "id"):
        val = (target.get(key) or "").strip()
        if val:
            candidates.append((val, key))

    for cand, how in candidates:
        if cand in tried:
            continue
        tried.append(cand)
        p = hashpin.msa_path_for(cand, msa_dir)
        if p is not None:
            return p, how
    return None, f"no usable a3m (tried {', '.join(repr(t) for t in tried)})"


def pins_for(target: dict[str, Any], msa_dir: Path, msa_sentinel: str,
             ) -> tuple[dict[str, Any], Path | None]:
    """Return ({...pin fields...}, msa_path_or_None) for one target.

    Never raises for a missing input: an unpinnable target comes back with the
    empty hash, a reason, and scoreable=False so it is visible in the output
    instead of vanishing from it.
    """
    modality = modality_of(target)
    pins: dict[str, Any] = {
        "name": target.get("protein_name") or target.get("gene_name") or target["id"],
        "modality": modality,
        "pinned_receptor_sha256": "",
        "pinned_msa_sha256": "",
        "scoreable": True,
    }
    msa_path: Path | None = None

    if modality == "protein":
        msa_path, how = resolve_msa(target, msa_dir)
        if msa_path is None:
            pins["pinned_receptor_sha256"] = hashpin.SHA256_EMPTY
            pins["pinned_msa_sha256"] = hashpin.SHA256_EMPTY
            pins["receptor_empty_reason"] = how
            pins["msa_empty_reason"] = how
            pins["scoreable"] = False
            return pins, None
        receptor = hashpin.sequence_from_msa(msa_path)
        if not receptor:
            reason = f"a3m {msa_path} has no query sequence line"
            pins["pinned_receptor_sha256"] = hashpin.SHA256_EMPTY
            pins["pinned_msa_sha256"] = hashpin.SHA256_EMPTY
            pins["receptor_empty_reason"] = reason
            pins["msa_empty_reason"] = reason
            pins["scoreable"] = False
            return pins, None
        pins["pinned_receptor_sha256"] = hashpin.sha256_text(receptor)
        pins["pinned_msa_sha256"] = hashpin.sha256_file(msa_path)
        pins["msa_file"] = str(msa_path)
        if how != "uniprot_id":
            pins["msa_resolved_by"] = how
        return pins, msa_path

    # Non-protein: no alignment file exists for this modality, by construction.
    if modality == "crispr":
        receptor = hashpin.crispr_receptor_material(SPCAS9_REC1_200AA,
                                                    SGRNA_SCAFFOLD_RNA)
        pins["receptor_covers"] = ["SPCAS9_REC1_200AA", "SGRNA_SCAFFOLD_RNA"]
    else:  # mrna
        rna = (target.get("rna_sequence") or "").strip()
        if not rna:
            reason = "mRNA target has no rna_sequence in targets.json"
            pins["pinned_receptor_sha256"] = hashpin.SHA256_EMPTY
            pins["pinned_msa_sha256"] = hashpin.SHA256_EMPTY
            pins["receptor_empty_reason"] = reason
            pins["msa_empty_reason"] = hashpin.NO_ALIGNMENT_REASON["mrna"]
            pins["scoreable"] = False
            return pins, None
        receptor = hashpin.mrna_receptor_material(rna)
        pins["receptor_covers"] = ["rna_sequence"]

    pins["pinned_receptor_sha256"] = hashpin.sha256_text(receptor)
    pins["pinned_msa_sha256"] = (hashpin.SHA256_EMPTY if msa_sentinel == "empty"
                                 else "none")
    pins["msa_empty_reason"] = hashpin.NO_ALIGNMENT_REASON[modality]
    return pins, None


def receptor_material(target: dict[str, Any], modality: str,
                      msa_dir: Path) -> tuple[str, Path | None]:
    """The exact string Rescorer hands verify_pins(), and the a3m it uses."""
    if modality == "protein":
        msa_path, _ = resolve_msa(target, msa_dir)
        if msa_path is None:
            raise hashpin.InputPinError(
                "MSA_DIR_MISSING", f"no usable a3m for {target['id']}")
        return (hashpin.sequence_from_msa(msa_path) or ""), msa_path
    if modality == "crispr":
        return hashpin.crispr_receptor_material(SPCAS9_REC1_200AA,
                                                SGRNA_SCAFFOLD_RNA), None
    return hashpin.mrna_receptor_material(
        (target.get("rna_sequence") or "").strip()), None


def verify(target: dict[str, Any], pins: dict[str, Any], msa_dir: Path) -> None:
    """Re-run the client's fail-closed gate against the files on disk.

    Raises hashpin.InputPinError exactly as the validator would at score time.
    An empty pin whose input actually exists is rejected here too: that is the
    "pinned to the empty hash but the input is real" failure mode.
    """
    hashpin.require_msa_dir(msa_dir)
    modality = pins["modality"]
    receptor, msa_path = receptor_material(target, modality, msa_dir)

    if receptor and pins["pinned_receptor_sha256"] == hashpin.SHA256_EMPTY:
        raise hashpin.InputPinError(
            "INPUT_HASH_MISMATCH",
            f"target {target['id']}: pinned receptor is the EMPTY hash but a "
            f"real receptor input exists (len={len(receptor)}) — refusing")
    if msa_path is not None and pins["pinned_msa_sha256"] == hashpin.SHA256_EMPTY:
        raise hashpin.InputPinError(
            "INPUT_HASH_MISMATCH",
            f"target {target['id']}: pinned MSA is the EMPTY hash but the "
            f"alignment file {msa_path} exists — refusing")

    hashpin.verify_pins(receptor_sequence=receptor, msa_path=msa_path,
                        pinned_receptor_sha256=pins["pinned_receptor_sha256"],
                        pinned_msa_sha256=pins["pinned_msa_sha256"],
                        target=target["id"])


def count_table(out: dict[str, dict[str, Any]]) -> str:
    """real-receptor / real-alignment / empty counts, per modality."""
    rows: dict[str, dict[str, int]] = {}
    for p in out.values():
        r = rows.setdefault(p["modality"], {"targets": 0, "real_receptor": 0,
                                            "real_msa": 0, "empty_receptor": 0,
                                            "empty_msa": 0})
        r["targets"] += 1
        empty_r = p["pinned_receptor_sha256"] in ("", hashpin.SHA256_EMPTY, "none")
        empty_m = p["pinned_msa_sha256"] in ("", hashpin.SHA256_EMPTY, "none")
        r["real_receptor" if not empty_r else "empty_receptor"] += 1
        r["real_msa" if not empty_m else "empty_msa"] += 1

    head = (f"{'modality':<10}{'targets':>9}{'real receptor':>15}"
            f"{'real alignment':>16}{'empty receptor':>16}{'empty alignment':>17}")
    lines = [head, "-" * len(head)]
    tot = {"targets": 0, "real_receptor": 0, "real_msa": 0,
           "empty_receptor": 0, "empty_msa": 0}
    for mod in sorted(rows):
        r = rows[mod]
        for k in tot:
            tot[k] += r[k]
        lines.append(f"{mod:<10}{r['targets']:>9}{r['real_receptor']:>15}"
                     f"{r['real_msa']:>16}{r['empty_receptor']:>16}"
                     f"{r['empty_msa']:>17}")
    lines.append("-" * len(head))
    lines.append(f"{'TOTAL':<10}{tot['targets']:>9}{tot['real_receptor']:>15}"
                 f"{tot['real_msa']:>16}{tot['empty_receptor']:>16}"
                 f"{tot['empty_msa']:>17}")
    return "\n".join(lines)


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
    ap.add_argument("--print-json", action="store_true",
                    help="print the emitted pins, one line per target")
    args = ap.parse_args(argv)

    msa_dir = Path(args.msa_dir)
    hashpin.require_msa_dir(msa_dir)          # fail closed before any work
    targets = json.loads(Path(args.targets).read_text())

    out: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for t in targets:
        if not is_active(t):
            continue
        try:
            pins, _ = pins_for(t, msa_dir, args.msa_sentinel)
        except TargetError as e:
            errors.append(f"{t.get('id')}: {e}")
            continue
        if not pins["scoreable"]:
            errors.append(f"{t['id']}: {pins.get('receptor_empty_reason')}")
        out[t["id"]] = pins

    Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    failures: list[str] = []
    if not args.no_verify:
        by_id = {t["id"]: t for t in targets}
        for tid, pins in out.items():
            if not pins["scoreable"]:
                continue              # already reported as a PIN-ERROR
            try:
                verify(by_id[tid], pins, msa_dir)
            except hashpin.InputPinError as e:
                failures.append(f"{tid}: [{e.reason_code}] {e.detail}")

    n_mod: dict[str, int] = {}
    for p in out.values():
        n_mod[p["modality"]] = n_mod.get(p["modality"], 0) + 1
    print(f"wrote {args.out}: {len(out)} targets {n_mod}")
    print(count_table(out))
    for e in errors:
        print(f"  [PIN-ERROR] {e}")
    if args.no_verify:
        print("  verification skipped (--no-verify)")
    elif failures:
        print(f"  FAIL-CLOSED CHECK: {len(failures)}/{len(out)} targets REJECTED")
        for f in failures:
            print(f"    [VERIFY-FAIL] {f}")
    else:
        n_ok = sum(1 for p in out.values() if p["scoreable"])
        print(f"  FAIL-CLOSED CHECK: all {n_ok}/{n_ok} scoreable targets PASS "
              f"(hashpin.verify_pins against files on disk)")

    if args.print_json:
        for tid in sorted(out):
            print(json.dumps({tid: out[tid]}, sort_keys=True))
    return 1 if (errors or failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
