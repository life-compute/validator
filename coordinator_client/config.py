"""Configuration for the coordinator validator client.

Every value is env-overridable.  Nothing secret is stored here — the keypair
is referenced by PATH only and read once by signing.Signer.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ── Coordinator endpoint ─────────────────────────────────────────────────────
COORDINATOR_URL = _env("COORDINATOR_URL", "https://coordinator.example.com").rstrip("/")

# ── Validator identity ───────────────────────────────────────────────────────
# The REAL key file the validator signs with.  Same file the on-chain daemon
# used (validator_daemon.VALIDATOR_KEYPAIR), so the wallet identity is
# unchanged by the coordinator migration.  Read ONCE at startup by
# signing.Signer.from_keypair_file(); never logged, never passed in argv.
VALIDATOR_KEYPAIR = _env("VALIDATOR_KEYPAIR", str(Path.home() / ".life-compute/wallet.json"))

WORKER_NAME = _env("WORKER_NAME", "") or None

# ── Paths ────────────────────────────────────────────────────────────────────
WORK_DIR = Path(_env("WORK_DIR", str(Path(__file__).resolve().parent.parent)))
OUTPUT_DIR = WORK_DIR / "output"
PROGRESS_DB = Path(_env("PROGRESS_DB", str(OUTPUT_DIR / "coordinator_progress.sqlite3")))
VERDICT_LOG = Path(_env("VERDICT_LOG", str(OUTPUT_DIR / "coordinator_verdicts.jsonl")))

# MSA library.  Fail-closed input: if this directory is missing, NOTHING is
# scored (see rescore.msa_dir_ok).
MSA_DIR = Path(_env("MSA_DIR", "/mnt/life-data/msa_files"))

# ── Polling / retry ──────────────────────────────────────────────────────────
POLL_SECONDS = int(_env("POLL_SECONDS", "30"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "30"))

# Capped exponential backoff for 5xx reads.  Bounded attempts — never an
# unbounded retry loop on a single item.
READ_MAX_ATTEMPTS = int(_env("READ_MAX_ATTEMPTS", "5"))
READ_BACKOFF_BASE = float(_env("READ_BACKOFF_BASE", "2.0"))
READ_BACKOFF_CAP = float(_env("READ_BACKOFF_CAP", "60.0"))

# Per-result attempt cap on OUR side.  The coordinator also caps at
# scoring.max_attempts=3; this stops us burning cycles locally first.
MAX_ITEM_ATTEMPTS = int(_env("MAX_ITEM_ATTEMPTS", "3"))

# ── Scoring ──────────────────────────────────────────────────────────────────
# Contract GET /config -> scoring.rescore_timeout_ms = 1_200_000 (20 minutes).
# A re-score that exceeds this is abandoned and reported as TIMEOUT.
RESCORE_TIMEOUT_S = float(_env("RESCORE_TIMEOUT_S", "1200"))

BOLTZ_SEED = int(_env("BOLTZ_SEED", "68"))

# Protein MSA subsampling depth.  Kept at the Boltz-2.2.1 default so the
# validator does not silently diverge from the miner (see report §1).
NUM_SUBSAMPLED_MSA = int(_env("NUM_SUBSAMPLED_MSA", "1024"))

GPU_BIAS_PATH = Path(_env("GPU_BIAS_PATH", str(OUTPUT_DIR / "gpu_bias_models.json")))
GPU_BIAS_MIN_SAMPLES = int(_env("GPU_BIAS_MIN_SAMPLES", "10"))

# Per-GPU error tables are built ONLY from audit rows at/after this instant.
# Before it, the `miner_gpu` field recorded the VALIDATOR's own card, not the
# submitting miner's hardware, so earlier rows are mis-attributed.
GPU_ATTRIBUTION_CUTOFF_UTC = "2026-09-18T12:00:00+00:00"

# Bias factors outside this range are treated as model corruption and ignored.
BIAS_FACTOR_LO = float(_env("BIAS_FACTOR_LO", "0.1"))
BIAS_FACTOR_HI = float(_env("BIAS_FACTOR_HI", "10.0"))


def host_only(url: str) -> str:
    """scheme://host of *url* — never the path, never the query string.

    Any log line naming an endpoint MUST route through this: coordinator and
    RPC URLs can carry credentials in the query string.
    """
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.hostname}" if p.hostname else "<unparseable>"
    except Exception:
        return "<unparseable>"
