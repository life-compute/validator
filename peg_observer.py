#!/usr/bin/env python3
"""
peg_observer.py — Stage 4: validator-side pegRNA observer. LOG-ONLY sidecar.

Tails the miner's pegRNA evaluation log, independently re-scores every
candidate with the Stage 3 evaluator (peg_json_eval.PegJsonModel), and records
miner-claimed vs validator-computed side by side so a tolerance can later be
chosen from REAL data — the same discipline as the CDK4/MYC CRISPR threshold
calibration (gather distributions first, pick thresholds second).

ISOLATION — this is a SIDECAR, not a daemon edit:
    * Runs as its own pm2 app (life-peg-observer). It does NOT import, patch,
      or restart validator_daemon.py.
    * Shares NO state with the validator: no queues, no GpuBiasTracker, no
      stats.json, no _SEEN_SUBMISSIONS, no rate-limit slots.
    * No Solana RPC, no validate_result, no mint_reward, no reward number.
    * Emits NO verdict: no CONFIRM, no REJECT, no tolerance, no threshold.
      Stage 4 produces deviation DATA only.
    * Writes exactly two files, both its own:
        output/peg_agreement.jsonl      (append-only results)
        output/peg_miner_log.pos        (byte offset, resume marker)

Protein / mRNA / CRISPR confirms, rewards, and rate limiting are structurally
untouched: there is no code path from this process to any of them.

STATEFULNESS: the only persisted state is a byte offset written by THIS
process. No daemon-owned JSON is read or written, so the GpuBiasTracker
whole-file-rewrite-from-memory trap (on-disk edits silently reverted while a
daemon holds the file) cannot apply here.

Usage:
    python3 peg_observer.py --once        # single pass, exit
    python3 peg_observer.py --watch       # poll forever (pm2 mode)
    python3 peg_observer.py --status      # counts + offset, no scoring
    python3 peg_observer.py --selftest    # prove the guards fire
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("peg-observer")

WORK_DIR = Path(__file__).parent
sys.path.insert(0, str(WORK_DIR))


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ── Inputs ────────────────────────────────────────────────────────────────────
# The miner's log. Source-agnostic on purpose: a local path, an rsync/syncthing
# target, or a `git fetch`ed checkout of the miner branch all work. No SSH
# dependency — the 5060 is publickey-denied from this host.
PEG_MINER_LOG = Path(_env("PEG_MINER_LOG", str(WORK_DIR / "peg_sync" / "life_peg_scores.jsonl")))
PEG_SPEC_PATH = Path(_env("PEG_SPEC_PATH", str(WORK_DIR / "peg_vendor" / "peg_spec.json")))
PEG_MODEL_PATH = Path(_env("PEG_MODEL_PATH", str(WORK_DIR / "peg_vendor" / "peg_model.json")))
PEG_VENDOR = WORK_DIR / "peg_vendor"

# Outputs — both owned exclusively by this process.
AGREEMENT_JSONL = WORK_DIR / "output" / "peg_agreement.jsonl"
POS_FILE = WORK_DIR / "output" / "peg_miner_log.pos"

POLL_SECONDS = int(_env("PEG_POLL_SECONDS", "60"))

# Pinned: must match Stage 3 exactly or the two sides are not comparable.
PEG_EXPECTED_MODEL_SHA256 = "3a873f545a957bfd204455e2205f07d5165748d7914e31a1af266bf19ef64aed"

# The miner's claimed value is rounded to 6 dp at write time
# (peg_predict_tp53.py: round(s, 6)). Comparing full-precision validator output
# against a 6-dp claim yields a spurious ~1e-7 floor — the exact artifact that
# masked true agreement during Stage 3 symmetry. Both sides are rounded to this
# many places before comparison, and the raw validator value is also recorded.
CLAIM_DECIMALS = 6

_MIN_FILE_BYTES = 2


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Guards — reused verbatim in behaviour from Stage 3
# ══════════════════════════════════════════════════════════════════════════════

def load_spec() -> dict | None:
    """[PEG-MISS] on absent/empty/invalid spec."""
    if not PEG_SPEC_PATH.exists():
        log.error(f"  [PEG-MISS] feature spec absent at {PEG_SPEC_PATH} — REFUSING to score.")
        return None
    if PEG_SPEC_PATH.stat().st_size < _MIN_FILE_BYTES:
        log.error(f"  [PEG-MISS] feature spec at {PEG_SPEC_PATH} is empty/truncated — REFUSING.")
        return None
    try:
        spec = json.loads(PEG_SPEC_PATH.read_text())
    except Exception as exc:
        log.error(f"  [PEG-MISS] spec is not valid JSON: {exc} — REFUSING to score.")
        return None
    if "feature_order" not in spec:
        log.error("  [PEG-MISS] spec missing feature_order — REFUSING to score.")
        return None
    return spec


def load_model():
    """[PEG-ARTIFACT-MISMATCH] on any artifact drift."""
    if not PEG_MODEL_PATH.exists():
        log.error(f"  [PEG-MISS] model artifact absent at {PEG_MODEL_PATH} — REFUSING to score.")
        return None
    if PEG_MODEL_PATH.stat().st_size < _MIN_FILE_BYTES:
        log.error(f"  [PEG-MISS] model artifact empty/truncated — REFUSING to score.")
        return None
    sha = _sha256_file(PEG_MODEL_PATH)
    if sha != PEG_EXPECTED_MODEL_SHA256:
        log.error(
            f"  [PEG-ARTIFACT-MISMATCH] {PEG_MODEL_PATH} sha256={sha[:16]}… but expected "
            f"{PEG_EXPECTED_MODEL_SHA256[:16]}… — REFUSING to score. Re-verify against "
            f"the miner rig; agreement data from a different model is not comparable."
        )
        return None
    try:
        from peg_json_eval import PegJsonModel  # type: ignore
        mdl = PegJsonModel(PEG_MODEL_PATH)
    except Exception as exc:
        log.error(f"  [PEG-MISS] cannot build evaluator: {exc} — REFUSING to score.")
        return None
    log.info(f"  [PEG-MODEL] loaded sha={sha[:16]} (matches 5060-reported)")
    return mdl


# Fields the miner's log row actually carries (peg_predict_tp53.py schema).
# window / edit_offset / pam_offset are NOT logged.
_LOGGED_CONTEXT = {
    "gene", "genomic_edit_pos", "nick_to_edit", "correction_length",
    "edit_to_rtt_end", "strand", "spacer", "pbs", "rtt", "edit_type",
}
# Features that CANNOT be reconstructed from a log row, because they require
# window / edit_offset / pam_offset.
_UNRECONSTRUCTABLE = {"pam_density", "pam_to_edit"}


def check_context_guard(spec: dict, mdl) -> bool:
    """
    [PEG-CONTEXT-MISSING] — the guard that prevents a future silent break.

    The miner's log omits window/edit_offset/pam_offset, so pam_density and
    pam_to_edit fall back to the _ABSENT sentinel (-1.0) when re-scored here.
    Measured on 112 real candidates: the feature vector differs on 112/112 rows,
    yet the score differs on 0/112 — because THIS model never splits on those
    two features (35 of 43 features are used in >=1 split; these are not).

    That agreement is a coincidence of the current model, not a property of the
    pipeline. A retrained model that splits on either feature would make every
    re-score silently wrong — the CDK4 mechanism exactly (a substitution that
    happens not to matter yet).

    So the coincidence is checked mechanically on every startup rather than
    trusted. If the loaded model splits on an unreconstructable feature, refuse
    loudly. This fires automatically on a retrain, with nobody needing to
    remember to re-check it.
    """
    order = list(spec["feature_order"])
    used = set()
    for tree in mdl.trees:
        for f in tree["feature"]:
            if f >= 0:
                used.add(f)
    offenders = [order[i] for i in sorted(used) if order[i] in _UNRECONSTRUCTABLE]
    if offenders:
        log.error(
            f"  [PEG-CONTEXT-MISSING] the loaded model splits on {offenders}, which cannot "
            f"be reconstructed from the miner's log schema (window/edit_offset/pam_offset "
            f"are not logged). Re-scoring would silently diverge — REFUSING to score. "
            f"Fix: have the miner add those fields to the log row."
        )
        return False
    log.info(
        f"  [PEG-CONTEXT-OK] model splits on {len(used)}/{len(order)} features; none "
        f"require the unlogged context fields {sorted(_UNRECONSTRUCTABLE)} — re-scoring "
        f"from the log is exact for this artifact."
    )
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Re-scoring
# ══════════════════════════════════════════════════════════════════════════════

def rescore_row(row: dict, spec: dict, mdl) -> tuple[float | None, str]:
    """Independently recompute efficiency for one miner log row."""
    if str(PEG_VENDOR) not in sys.path:
        sys.path.insert(0, str(PEG_VENDOR))
    try:
        from adaptive.life_peg import featurize_pegrna, PEG_FEATURE_NAMES  # type: ignore
    except Exception as exc:
        return None, f"extractor import failed: {exc}"

    if list(PEG_FEATURE_NAMES) != list(spec["feature_order"]):
        return None, "feature order drift between extractor and spec"

    vec = featurize_pegrna(
        row.get("spacer", ""), row.get("pbs", ""), row.get("rtt", ""),
        row.get("edit_type", ""),
        gene=row.get("gene"),
        genomic_edit_pos=row.get("genomic_edit_pos"),
        nick_to_edit=row.get("nick_to_edit"),
        correction_length=row.get("correction_length"),
        edit_to_rtt_end=row.get("edit_to_rtt_end"),
        window=row.get("window"),
        edit_offset=row.get("edit_offset"),
        pam_offset=row.get("pam_offset"),
        strand=row.get("strand", "+"),
    )
    if vec is None:
        return None, "featurize_pegrna returned None (invalid spacer/pbs/rtt/edit_type)"
    try:
        return mdl.predict(vec), "ok"
    except Exception as exc:
        return None, f"evaluation failed: {exc}"


def _read_pos() -> int:
    try:
        return int(POS_FILE.read_text().strip())
    except Exception:
        return 0


def _write_pos(n: int) -> None:
    POS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POS_FILE.write_text(str(n))


def process_once(spec: dict, mdl, *, dry_run: bool = False) -> dict:
    """
    Consume new lines from the miner log, re-score, append agreement rows.

    Offset-tracked (same pattern as crank_queue.jsonl.pos) so restarts resume
    instead of re-processing. A truncated/rotated log resets the offset.
    """
    stats = {"new": 0, "scored": 0, "refused": 0, "parse_err": 0}

    if not PEG_MINER_LOG.exists():
        log.warning(
            f"  [PEG-MISS] miner log not present at {PEG_MINER_LOG} — nothing to observe. "
            f"This is expected until the miner starts writing "
            f"output/life_peg_scores.jsonl; no data is fabricated in the meantime."
        )
        return stats

    size = PEG_MINER_LOG.stat().st_size
    pos = _read_pos()
    if pos > size:
        log.warning(f"  [PEG-LOG-ROTATED] offset {pos} > size {size} — restarting from 0.")
        pos = 0
    if pos == size:
        return stats

    out_rows = []
    with PEG_MINER_LOG.open() as fh:
        fh.seek(pos)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            stats["new"] += 1
            try:
                row = json.loads(line)
            except Exception:
                stats["parse_err"] += 1
                log.error("  [PEG-FEATURE-ERROR] unparseable JSON line in miner log — skipped.")
                continue

            claimed = row.get("predicted_efficiency")
            rescored, note = rescore_row(row, spec, mdl)

            if rescored is None:
                stats["refused"] += 1
                log.error(f"  [PEG-FEATURE-ERROR] {note} — row refused, no value recorded.")
                out_rows.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "mode": "LOG_ONLY", "status": "REFUSED", "note": note,
                    "gene": row.get("gene"), "hotspot": row.get("hotspot"),
                    "claimed": claimed, "rescored": None,
                })
                continue

            stats["scored"] += 1
            # Round both sides to the miner's write precision before comparing.
            r_cmp = round(rescored, CLAIM_DECIMALS)
            c_cmp = round(float(claimed), CLAIM_DECIMALS) if claimed is not None else None
            dev = abs(r_cmp - c_cmp) if c_cmp is not None else None
            rel = (dev / abs(c_cmp)) if (dev is not None and c_cmp) else None

            out_rows.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "mode": "LOG_ONLY", "status": "SCORED",
                "gene": row.get("gene"), "hotspot": row.get("hotspot"),
                "edit_type": row.get("edit_type"), "strand": row.get("strand"),
                "spacer": row.get("spacer"), "pbs": row.get("pbs"), "rtt": row.get("rtt"),
                "claimed": c_cmp,
                "rescored": r_cmp,
                "rescored_full": rescored,
                "abs_dev": dev,
                "rel_err": rel,
                "model_sha": PEG_EXPECTED_MODEL_SHA256[:16],
                # No verdict, no tolerance, no threshold — Stage 4 is data only.
            })
        new_pos = fh.tell()

    if dry_run:
        log.info(f"  [PEG-DRY-RUN] {stats} (offset not advanced, nothing written)")
        return stats

    if out_rows:
        AGREEMENT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with AGREEMENT_JSONL.open("a") as fh:
            for r in out_rows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    _write_pos(new_pos)

    if stats["new"]:
        log.info(
            f"  [PEG-OBSERVE] new={stats['new']} scored={stats['scored']} "
            f"refused={stats['refused']} parse_err={stats['parse_err']} -> {AGREEMENT_JSONL}"
        )
    return stats


def _status() -> int:
    print(f"  miner log   : {PEG_MINER_LOG}")
    print(f"    exists    : {PEG_MINER_LOG.exists()}"
          + (f"  size={PEG_MINER_LOG.stat().st_size}" if PEG_MINER_LOG.exists() else ""))
    print(f"  offset      : {_read_pos()}")
    print(f"  agreement   : {AGREEMENT_JSONL}")
    if AGREEMENT_JSONL.exists():
        n = sum(1 for _ in AGREEMENT_JSONL.open())
        print(f"    rows      : {n}")
    else:
        print("    rows      : 0 (file not created yet)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 4 pegRNA observer — log-only sidecar")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.status:
        return _status()
    if args.selftest:
        from peg_observer_selftest import run_selftest  # type: ignore
        return run_selftest()

    spec = load_spec()
    if spec is None:
        return 2
    mdl = load_model()
    if mdl is None:
        return 2
    if not check_context_guard(spec, mdl):
        return 2

    if args.watch:
        log.info(
            f"[PEG-OBSERVER] watching {PEG_MINER_LOG} every {POLL_SECONDS}s "
            f"— LOG-ONLY, no verdicts, no chain calls"
        )
        while True:
            try:
                process_once(spec, mdl)
            except Exception as exc:
                log.error(f"[PEG-OBSERVER] unhandled error (continuing): {exc}")
            time.sleep(POLL_SECONDS)

    process_once(spec, mdl, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
