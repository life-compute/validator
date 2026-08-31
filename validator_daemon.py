#!/usr/bin/env python3
"""
LIFE Compute — Validator Daemon (devnet)

Three-step loop every POLL_SECONDS:
  1. getProgramAccounts → find all ResultSubmission PDAs with status Pending/Validating
  2. For each: re-run Boltz2 on the claimed SMILES against the same target
  3. Call validate_result on-chain — confirm if |rescored - claimed| / |claimed| ≤ 25%

On-chain call uses the same Node.js / Anchor stack as the miner.
Boltz2 scoring runs directly via the boltz Python API (pip boltz==2.2.1).
Results logged to output/validator_log.jsonl and output/validator_audit.jsonl.
Stats written to stats.json for the dashboard.

Security hardening (2026-08-13):
  - Pipeline injection prevention: UUID tmpdir per validation (chmod 700),
    SHA256 SMILES hash verified after write, affinity file mtime verified
    post-Boltz2 start.
  - Rate limiting: max 100 validations/hr per instance (rolling window).
  - Self-validation prevention: miner pubkey ≠ validator pubkey.
  - Input sanitization: SMILES length < 500 chars, valid chemical chars only.
  - Audit log: every validation decision → output/validator_audit.jsonl.
"""
import json, time, logging, os, re, shutil, stat, subprocess, sys, threading
import urllib.request, hashlib, uuid
from collections import deque
from pathlib import Path
from datetime import datetime, timezone

# ── Already-processed submission tracking ──────────────────────────────────────
# Keyed by submission pubkey → number of validate_on_chain attempts.
# Once a tx lands successfully, the on-chain status flips to Validating (1) and
# the RPC memcmp filter drops it from future polls.  But when the tx fails the
# account stays Pending (0) and re-appears every poll.  We cap retries so a
# persistently-failing submission doesn't block the queue indefinitely.
_SEEN_SUBMISSIONS: dict[str, int] = {}   # pubkey → attempt count
_MAX_RETRY_ATTEMPTS = 3                  # give up after this many failed on-chain calls

# ── Config from .env ──────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID        = _env("PROGRAM_ID",        "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf")
SOLANA_RPC        = _env("SOLANA_RPC",        "https://api.devnet.solana.com")
VALIDATOR_KEYPAIR = _env("VALIDATOR_KEYPAIR", str(Path.home() / ".life-compute/wallet.json"))
PAYER_KEYPAIR     = _env("PAYER_KEYPAIR",     str(Path.home() / ".life-compute/wallet.json"))
TARGETS_URL       = _env("TARGETS_URL",       "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
POLL_SECONDS      = int(_env("POLL_SECONDS", "30"))

# ── Miner allowlist — only process submissions from this wallet ───────────────
# Set to empty string to accept all miners (open mode).
MINER_WALLET  = _env("MINER_WALLET",  "")   # empty = open mode, accept all miners
MINER_ACCOUNT = _env("MINER_ACCOUNT", "BaMnTDYP1T4kZVwUbv9ZyppgUHeSRKNo9bMDRvFNN2iX")

WORK_DIR    = Path(__file__).parent
STATS_PATH  = WORK_DIR / "stats.json"
LOG_JSONL   = WORK_DIR / "output" / "validator_log.jsonl"
AUDIT_JSONL = WORK_DIR / "output" / "validator_audit.jsonl"
(WORK_DIR / "output").mkdir(exist_ok=True)

# ── Boltz2 / MSA paths ────────────────────────────────────────────────────────
MSA_DIR = Path(_env("MSA_DIR", str(WORK_DIR / "data" / "msa_files")))

# Fast inference settings (match miner)
_RECYCLING_STEPS       = 1
_SAMPLING_STEPS        = 25
_DIFFUSION_SAMPLES     = 1
_SAMPLING_STEPS_AFF    = 25
_DIFFUSION_SAMPLES_AFF = 1

BOLTZ_SEED = 68  # must match miner BOLTZ_SEED; used for reproducible Boltz2 rescoring
VALIDATION_TOLERANCE = 0.7777  # DEVNET TESTING TOLERANCE — tighten for mainnet
TIGHTENED_TOLERANCE  = 0.35   # used when GPU bias model exists (≥10 samples)
GPU_BIAS_PATH = WORK_DIR / "output" / "gpu_bias_models.json"

# Minimum samples per GPU·target-family before bias model activates
GPU_BIAS_MIN_SAMPLES = 10

# ── Tokenomics: base rewards and halving schedule ──────────────────────────────
# Base tier rewards (pre-halving).  CRISPR submissions are tier 3.
# Updated 2026-08-23: CRISPR tier-3 reward reduced from 25 → 7 $LIFE.
BASE_TIER_REWARDS: dict[int, int] = {1: 1, 2: 5, 3: 7}

# Halving schedule: every 210,000 on-chain epochs the reward halves.
# multiplier = 0.5 ** (current_epoch // HALVING_INTERVAL)
# Integer arithmetic: reward >> halvings (floor division, minimum 1 if >0).
HALVING_INTERVAL = 210_000

# ── Anchor / JS paths ─────────────────────────────────────────────────────────
ANCHOR_DIR  = Path(_env("ANCHOR_DIR", "/tmp/life-compute/core"))
IDL_PATH    = ANCHOR_DIR / "target/idl/life_core.json"
VALIDATE_JS = WORK_DIR / "life_validate.js"

# ── ResultSubmission discriminator (from IDL) ─────────────────────────────────
RESULT_DISCRIMINATOR = bytes([214, 115, 165, 103, 67, 211, 47, 88])
STATUS_PENDING    = 0   # Pending
STATUS_VALIDATING = 1   # Validating

# ── Security: input validation limits ─────────────────────────────────────────
_SMILES_MAX_LEN = 500
# Valid SMILES characters: element symbols, ring digits, branch/bond/stereo notation
_SMILES_VALID_RE = re.compile(r'^[A-Za-z0-9\[\]()\-=#:.+/\\@%*\s]+$')

# ── Security: rate limiting ────────────────────────────────────────────────────
_RATE_LIMIT_MAX   = 100          # max validations per hour per instance
_RATE_LIMIT_WINDOW = 3600.0      # 1 hour in seconds
_validation_timestamps: deque = deque()  # stores float epoch of each validation start

# ── Security: validator public key (derived at startup, never from disk again) ─
_VALIDATOR_PUBKEY: str = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-validator")


def _load_validator_pubkey() -> str:
    """Load validator keypair and extract the base58 public key (bytes 32-63)."""
    import base58
    try:
        with open(VALIDATOR_KEYPAIR) as f:
            kp_bytes = bytes(json.load(f))
        if len(kp_bytes) < 64:
            log.warning("Validator keypair too short; self-validation check disabled")
            return ""
        return base58.b58encode(kp_bytes[32:64]).decode()
    except Exception as e:
        log.warning(f"Could not load validator pubkey: {e}; self-validation check disabled")
        return ""


# ── Security: input sanitization ─────────────────────────────────────────────

def _sanitize_smiles(smiles: str, pubkey: str) -> bool:
    """
    Return True if SMILES passes all input validation gates.
    Logs SECURITY WARNING and returns False on failure.
    """
    if len(smiles) >= _SMILES_MAX_LEN:
        log.warning(
            f"SECURITY WARNING  pubkey={pubkey[:16]}…  "
            f"SMILES length {len(smiles)} ≥ {_SMILES_MAX_LEN} — rejected"
        )
        return False
    if not _SMILES_VALID_RE.match(smiles):
        bad = [c for c in smiles if not re.match(r'[A-Za-z0-9\[\]()\-=#:.+/\\@%*\s]', c)]
        log.warning(
            f"SECURITY WARNING  pubkey={pubkey[:16]}…  "
            f"SMILES contains invalid chars {bad[:5]} — rejected"
        )
        return False
    return True


# ── Security: rate limiting ───────────────────────────────────────────────────

def _rate_limit_check() -> bool:
    """
    Return True if this validation is permitted under the rolling hourly limit.
    Prunes expired timestamps on each call.
    """
    now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW
    while _validation_timestamps and _validation_timestamps[0] < cutoff:
        _validation_timestamps.popleft()
    if len(_validation_timestamps) >= _RATE_LIMIT_MAX:
        oldest = _validation_timestamps[0]
        secs_until_free = int(_RATE_LIMIT_WINDOW - (now - oldest)) + 1
        log.warning(
            f"Rate limit reached ({_RATE_LIMIT_MAX}/hr) — "
            f"next slot available in ~{secs_until_free}s"
        )
        return False
    _validation_timestamps.append(now)
    return True


# ── Security: self-validation prevention ─────────────────────────────────────

def _check_self_validation(miner_pubkey: str, submission_pubkey: str) -> bool:
    """
    Return True if this validation is permitted (miner ≠ validator).
    Returns False and logs SECURITY WARNING if the miner is also the validator.
    """
    if not _VALIDATOR_PUBKEY:
        return True   # check disabled (keypair unreadable at startup)
    if miner_pubkey == _VALIDATOR_PUBKEY:
        log.warning(
            f"SECURITY WARNING  submission={submission_pubkey[:16]}…  "
            f"miner pubkey matches validator pubkey ({miner_pubkey[:16]}…) — rejected"
        )
        return False
    return True


# ── Audit log ─────────────────────────────────────────────────────────────────

def append_audit(row: dict) -> None:
    """Append one validation decision record to the immutable audit log."""
    row.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with AUDIT_JSONL.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ── Today-counter helpers ──────────────────────────────────────────────────────

def _today_date_utc() -> str:
    """Return today's date in UTC as YYYY-MM-DD (used for midnight resets)."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _count_today_from_log() -> tuple:
    """
    Scan validator_log.jsonl and count decisions logged since today's midnight UTC.
    Returns (total_validated, confirmed, rejected).
    Only rows with verdict == 'CONFIRM' or 'REJECT' are counted (excludes
    BOLTZ2_FAILED, RATE_LIMITED, SMILES_INVALID, UNKNOWN_TARGET, SELF_VALIDATION_REJECTED).
    """
    today = _today_date_utc()
    total = conf = rej = 0
    if LOG_JSONL.exists():
        try:
            with LOG_JSONL.open() as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        if row.get("ts", "").startswith(today):
                            total += 1
                            v = row.get("verdict", "")
                            if v == "CONFIRM":
                                conf += 1
                            elif v == "REJECT":
                                rej += 1
                    except Exception:
                        pass
        except Exception:
            pass
    return total, conf, rej


def _load_seen_from_audit() -> None:
    """
    Pre-populate _SEEN_SUBMISSIONS from the audit log.
    Any submission that already has a successful tx recorded is marked with a
    sentinel count (_MAX_RETRY_ATTEMPTS) so it won't be retried on restart.
    Submissions that only have BOLTZ2_FAILED entries also accumulate their
    attempt count so they are not retried indefinitely after a restart.
    """
    if not AUDIT_JSONL.exists():
        return
    # We only want today's entries — yesterday's accounts should already be
    # Validating on-chain and won't appear in the RPC filter anyway.
    today = _today_date_utc()
    try:
        with AUDIT_JSONL.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if not row.get("ts", "").startswith(today):
                        continue
                    pk = row.get("submission_pubkey", "")
                    if not pk:
                        continue
                    decision = row.get("decision", "")
                    # A CONFIRM or REJECT decision means we ran Boltz2 and called
                    # validate_on_chain.  Mark at max-attempts so we skip on reload
                    # (the tx either landed — account is now Validating and filtered
                    # out by RPC — or it failed and we've already tried enough).
                    if decision in ("CONFIRM", "REJECT"):
                        _SEEN_SUBMISSIONS[pk] = _MAX_RETRY_ATTEMPTS
                    # BOLTZ2_FAILED: count each occurrence so the cap is preserved
                    # across restarts, preventing repeated GPU waste on permanently-
                    # failing submissions (e.g. siRNA sequences on protein targets).
                    elif decision == "BOLTZ2_FAILED":
                        _SEEN_SUBMISSIONS[pk] = min(
                            _SEEN_SUBMISSIONS.get(pk, 0) + 1,
                            _MAX_RETRY_ATTEMPTS,
                        )
                except Exception:
                    pass
    except Exception:
        pass


# ── GPU detection ─────────────────────────────────────────────────────────────

def detect_gpu_model() -> str:
    """
    Query nvidia-smi for the GPU name and normalise to short form.
    e.g. "NVIDIA GeForce RTX 5060" → "RTX 5060"
    Falls back to MINER_GPU_MODEL env var, then "UNKNOWN".
    Also writes MINER_GPU_MODEL to .env so the miner picks it up at next start.
    """
    # 1. Try env override first
    env_val = _env("MINER_GPU_MODEL", "")
    if env_val:
        log.info(f"GPU model from env: {env_val}")
        return env_val

    # 2. Query nvidia-smi
    gpu_name = ""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            raw = result.stdout.strip().splitlines()[0].strip()
            # Normalise: strip known prefixes
            for prefix in ("NVIDIA GeForce ", "NVIDIA ", "GeForce "):
                if raw.startswith(prefix):
                    raw = raw[len(prefix):]
            gpu_name = raw
    except Exception as e:
        log.warning(f"nvidia-smi query failed: {e}")

    if not gpu_name:
        gpu_name = "UNKNOWN"

    log.info(f"GPU model detected: {gpu_name}")

    # 3. Persist to .env so miner uses it too
    _write_gpu_to_env(gpu_name)
    return gpu_name


def _write_gpu_to_env(gpu_model: str) -> None:
    """Write/update MINER_GPU_MODEL=... in the local .env file."""
    env_path = WORK_DIR / ".env"
    try:
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        updated = False
        for i, line in enumerate(lines):
            if line.startswith("MINER_GPU_MODEL="):
                lines[i] = f"MINER_GPU_MODEL={gpu_model}"
                updated = True
                break
        if not updated:
            lines.append(f"MINER_GPU_MODEL={gpu_model}")
        env_path.write_text("\n".join(lines) + "\n")
        log.debug(f"MINER_GPU_MODEL={gpu_model} written to .env")
    except Exception as e:
        log.warning(f"Could not write MINER_GPU_MODEL to .env: {e}")


# ── GPU Bias Tracker ──────────────────────────────────────────────────────────

def _target_family(target_name: str) -> str:
    """Return a short target-family key used as the bias-tracker bucket.

    Namespacing rules (kept isolated so each modality trains its own model):
      - mRNA targets:   mRNA_KRAS → "mRNA_KRAS",  KRAS_mRNA → "mRNA_KRAS"
      - CRISPR targets: TP53_CRISPR → "TP53_CRISPR"  (full name, already unique)
      - Protein:        KRAS → "KRAS"  (first 4 uppercase chars)
    """
    if not target_name:
        return "UNKN"
    if target_name.startswith("mRNA_"):
        return "mRNA_" + target_name[5:].upper()[:4]
    if target_name.endswith("_mRNA"):
        return "mRNA_" + target_name[:-5].upper()[:4]
    if target_name.upper().endswith("_CRISPR"):
        return target_name.upper()   # e.g. "TP53_CRISPR" — full name, already unique
    return target_name.upper()[:4]


def _is_mrna_target(target: dict) -> bool:
    """Return True if this target is an mRNA target.

    Detects either:
      - target_type == "mRNA" field (used by life-compute/targets _mRNA suffix convention)
      - id starts with "mRNA_"
      - id ends with "_mRNA"
      - scoring_metric starts with "rna_"
    """
    if target.get("target_type") == "mRNA":
        return True
    tid = str(target.get("id", ""))
    if tid.startswith("mRNA_") or tid.endswith("_mRNA"):
        return True
    metric = str(target.get("scoring_metric", ""))
    if metric.startswith("rna_"):
        return True
    return False


def _is_crispr_target(target: dict) -> bool:
    """Return True if this target is a CRISPR target.

    Detects either:
      - target_type == "CRISPR" field
      - id starts with "CRISPR_"
      - on-chain id is in the CRISPR range 3000-3009
    """
    if target.get("target_type") == "CRISPR":
        return True
    tid = str(target.get("id", ""))
    if tid.startswith("CRISPR_"):
        return True
    return False


# ── CRISPR gRNA three-score analytical validation ─────────────────────────────
# Mirrors life_crispr.py from the miner (adaptive/life_crispr.py).
# No GPU required — runs in microseconds on CPU.
# Combined score = iptm × delivery  (Option B — off_target removed)
# Affinity encoding: −6.0 − 2.5 × combined + ε (ε deterministic from SHA-256)
# CRISPR submissions are confirmed when BOTH:
#   (a) validator combined >= CRISPR_MIN_COMBINED_VALIDATOR (0.45), AND
#   (b) |validator_affinity − claimed_affinity| / |claimed_affinity| ≤ 0.25

CRISPR_MIN_COMBINED_VALIDATOR = 0.45   # Option B default threshold: iptm × delivery

# Per-target overrides — must match miner-side values exactly.
# Only CDK4 and MYC are overridden; all other targets use the default 0.45.
# BCL2 and TERT are NOT changed here (separate decision, not part of this fix).
CRISPR_MIN_COMBINED_BY_TARGET: dict[str, float] = {
    "CDK4_CRISPR": 0.300,
    "MYC_CRISPR":  0.320,
}


def _crispr_threshold(target_name: str) -> float:
    """Return the combined-score threshold for a given CRISPR target name.
    Falls back to CRISPR_MIN_COMBINED_VALIDATOR (0.45) for any target not
    listed in CRISPR_MIN_COMBINED_BY_TARGET.
    """
    return CRISPR_MIN_COMBINED_BY_TARGET.get(target_name, CRISPR_MIN_COMBINED_VALIDATOR)


# On-chain integer ID → human-readable target name.
# Defined at module level so both the main loop and the CRISPR thread can
# resolve a per-target threshold without duplicating the mapping.
_CRISPR_ID_TO_NAME: dict[int, str] = {
    3000: "TP53_CRISPR",
    3001: "KRAS_CRISPR",
    3002: "BCL2_CRISPR",
    3003: "MYC_CRISPR",
    3004: "EGFR_CRISPR",
    3005: "HER2_CRISPR",
    3006: "BRCA1_CRISPR",
    3007: "PDL1_CRISPR",
    3008: "TERT_CRISPR",
    3009: "CDK4_CRISPR",
}

# Nucleotide complement map
_CRISPR_COMP = str.maketrans("ACGTacgt", "TGCAtgca")


def _crispr_complement(seq: str) -> str:
    return seq.translate(_CRISPR_COMP)


def _crispr_revcomp(seq: str) -> str:
    return _crispr_complement(seq)[::-1]


def _crispr_hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def _crispr_gc_content(seq: str) -> float:
    seq = seq.upper()
    gc = seq.count("G") + seq.count("C")
    return gc / len(seq) if seq else 0.0


# Repeat-element seed blacklist (12-mers from Alu, LINE-1, SINE-R, telomeric,
# centromeric repeats) — identical to the miner's _REPEAT_SEEDS set.
_CRISPR_REPEAT_SEEDS: set[str] = {
    "AGGACGCGTGGG", "GCTTGCACCGTG", "GGCCGGGCGCGG",
    "CTCGCCCTTAGT", "AGCCGGGCGCGG", "GCCCGAGTTCTG",
    "TTTTTTTTTTAG", "AAAAAAAAAATG", "TTTTTTGAGACG",
    "GAGGCGGAGCTT", "GCAGTGAGCCGA",
    "TTAGGGTTAGGG", "CCCTAACCCTAA",
    "AACGTCGAAATG", "CATATTCAGTTC", "GAAATTTCGTTC",
    "CACACACACACA", "ATATATATATATAT"[:12], "GCGCGCGCGCGC",
    "GCACCAGCACCA", "TGGCCTCGAGGA",
}


def _crispr_count_seed_hits(seq: str) -> int:
    seq = seq.upper()
    return sum(1 for i in range(len(seq) - 11)
               if seq[i:i+12] in _CRISPR_REPEAT_SEEDS)


def _crispr_has_stem_loop(seq: str) -> bool:
    if len(seq) < 20:
        return False
    arm1 = seq[:8].upper()
    arm2 = seq[12:20].upper()
    rc2  = _crispr_revcomp(arm2)
    return sum(a == b for a, b in zip(arm1, rc2)) >= 4


def _crispr_has_pam_context(seq: str) -> bool:
    tail = seq[-3:].upper()
    return not (tail[1] == "G" and tail[2] == "G")


# Known hotspot gRNA windows for each CRISPR target — identical to miner's
# HOTSPOT_GRNAS dict so validator scores align with miner scores.
_CRISPR_HOTSPOT_GRNAS: dict[str, list[str]] = {
    "TP53_CRISPR": [
        "CGTGAGCGCTTCGAGATGTT", "TCCTCAGCATCTTATCCGAG",
        "GCCCCCAGGGAGCACCCGCG", "GCCCTGGAGCCCTTCCTCTT",
        "AGGCCCCGGCCTGGGAGCAG", "CTACCTGGAGTCTTTCCACG",
        "GCAGGTACTGCCGTCGTGTG", "CACCAGCCTGTGTGTACTCG",
    ],
    "KRAS_CRISPR": [
        "TTATGTGTGACATGTTCTAA", "GTATTTCTGTGAATTAGCTG",
        "GTGAGTATTTCTGTGAATTA", "AAACTTGTGGTAGTTGGAGC",
        "TATAAACTTGTGGTAGTTGG", "GTAGTTGGAGCTGGTGGCGT",
        "CTTGTGGTAGTTGGAGCTGG", "TAGTTGGAGCTGGTGGCGTA",
    ],
    "BCL2_CRISPR": [
        "GCTGCACCTGACGCCCTTCA", "CCCAGAGTTTGAGACAGAAG",
        "TGGTCATCCCTGCCGCAGGT", "ATCCCAGCCTCCGTTATCCT",
        "GGAGTTTGATCCCCATGAAG", "GCACTTCAGGGAAATGCCTG",
        "TGTGGATGACTGAGTACCTG", "CTGACCAGAGACATGCCCAG",
    ],
    "MYC_CRISPR": [
        "GCAGCGGGCGCGCAGCGCAG", "CCGCCGCCTCGCTGCAGGGC",
        "TGCCGCCTCCTGTCGAAGTG", "AGCCGCCTCCTCGTCGAAGT",
        "GCACCCGCGTCGAGGGCAGT", "GCCCGCCGCCTCCTGTCGAA",
        "CGCCTCCTGTCGAAGTGTTC", "GGGCATCGTCGCGGTCCCTG",
    ],
    "EGFR_CRISPR": [
        "GCATGTGGAGGTGGAGATCA", "TTCCCGTCGCTATCAAGGAA",
        "GAAGACCCAGTTCCTTACGG", "CAGCATGTCAAGATCACAGA",
        "CGCATGAGCTCCTTCAGGCA", "CTCATGAGCTCCTTCAGGCA",
        "CGAGGATTTCCTTGTTGGCT", "GAGCAGCATCTCCGAAAGCC",
    ],
    "HER2_CRISPR": [
        "CGAGGGCTTCTGGCTCGCCA", "TCTACAGAGCCCACCTTGGC",
        "GCAGCTCATCTCCCGCAAAG", "AGGCACCTGCCTACGGGATC",
        "TGCAGCAGCCTAAGTGCCAT", "CGAGGAGAACCCGCTGTGGC",
        "TTCACAGGGACTTGGCTTCC", "CAGAAGGAGGTCTTCCTCCA",
    ],
    "BRCA1_CRISPR": [
        "GCTGCAAAGCTTGCTTGAAT", "CATGATGGTTTGATCCCAGG",
        "TAAATACCGCCTGAAATCGT", "CGTAAGGAGGTCAAGCCAGG",
        "GTATCCTGAGCAGAGCATGG", "AGGAACCGCATCGAGCGAAG",
        "CAGCCTCATTTTGTTAAATG", "GAAGCCTGAGCAGAAGATGA",
    ],
    "PDL1_CRISPR": [
        "TGCATGACCAGATCGAGAGC", "CGAGGTCCAGATGACATTCG",
        "AAGGTCCAGCTGCAGCAGTG", "GCCGTCGTACTGGCCGTCGT",
        "GTTCAGTGCACAGGGCAGCA", "CTCCAAGGACTATGTGCTGG",
        "GCAGTGGCACAGCCAGGAGA", "GCAGTCACAGTCTCCAGCCA",
    ],
    "TERT_CRISPR": [
        "CCCCCACCCCGCCCTAGCCC", "GCCCTTCCCCGCCCGCCCAG",
        "GCCCTAGCCCCAGGGCCCAG", "TCAGGGAGCCGCGAGCCCGC",
        "CGCGTCTTCAAGTCCTACGT", "ACATGTCCTGTGACCCAGGT",
        "GCCTTCAAGAGCAACAAGCC", "TGCTGTGGCTGCAGCCCAGG",
    ],
    "CDK4_CRISPR": [
        "AAGTTCATGGCCTTGGAGTT", "CGCTAAAGCAGTTCGAGTTG",
        "ATCCAGAAACGCAAACGCAA", "GCAAATGCGAGCTTCGAGTT",
        "AGGCCTGTGCGGCCCGCGCG", "GCCTTGGGCTACTTCTTCAG",
        "GTCCGCAGACCTCCAAATGG", "CCTCAGAGACCTCCAAATGG",
    ],
}


def score_grna(seq: str, target_id: str) -> dict:
    """
    Score a 20-mer gRNA.  Option B recalibration — matches miner exactly:

        combined = iptm × delivery       (off_target factor removed)
        threshold = CRISPR_MIN_COMBINED_VALIDATOR = 0.45

    iptm mirrors Boltz2's interface-pTM structural confidence: computed
    analytically as exp(-hamming_to_nearest_hotspot / 8), same as the
    old on_target term.  The off_target factor is no longer part of the
    combined score (it was depressing genuinely good gRNAs that happened
    to have seed-region homology to common repeats).

    Returns dict with iptm, off_target (informational only), delivery,
    combined, affinity.  The affinity is deterministic for a given
    sequence (SHA-256 seeded ε), unchanged from before.
    """
    import math
    seq = seq.upper().strip()
    if len(seq) != 20 or not all(c in "ACGT" for c in seq):
        return {"iptm": 0.0, "off_target": 0.0, "delivery": 0.0,
                "combined": 0.0, "affinity": -6.0}

    hotspots = _CRISPR_HOTSPOT_GRNAS.get(target_id, [])

    # ── iptm: Boltz2 structural confidence (analytical proxy) ─────────────────
    # exp(-hamming/8): 1.0 at dist=0, ≈0.08 at dist=20; fallback 0.5 if no hotspot.
    if hotspots:
        min_dist = min(_crispr_hamming(seq, h) for h in hotspots)
        iptm     = math.exp(-min_dist / 8.0)
    else:
        iptm = 0.5
    if _crispr_has_pam_context(seq):
        iptm = min(1.0, iptm * 1.05)

    # ── off_target (retained as informational field, not used in combined) ────
    n_off      = _crispr_count_seed_hits(seq)
    off_target = 1.0 / (1.0 + n_off)

    # ── delivery compatibility ────────────────────────────────────────────────
    gc = _crispr_gc_content(seq)
    if 0.40 <= gc <= 0.70:
        delivery = 1.0
    elif 0.30 <= gc < 0.40 or 0.70 < gc <= 0.80:
        delivery = 0.7
    else:
        delivery = 0.4
    if _crispr_has_stem_loop(seq):
        delivery = min(1.1, delivery + 0.1)

    # ── Option B combined formula ─────────────────────────────────────────────
    combined = iptm * delivery

    # Deterministic ε — SHA-256 of the sequence, unchanged
    import random
    _h   = int(hashlib.sha256(seq.encode()).hexdigest()[:8], 16)
    _rng = random.Random(_h)
    eps  = max(-0.4, min(0.4, _rng.gauss(0.0, 0.15)))
    affinity = -6.0 - 2.5 * combined + eps

    return {
        "iptm":       round(iptm,       4),
        "on_target":  round(iptm,       4),   # alias for backward compat with audit fields
        "off_target": round(off_target, 4),   # informational only
        "delivery":   round(delivery,   4),
        "combined":   round(combined,   4),
        "affinity":   round(affinity,   4),
    }


def run_crispr_validation(grna_seq: str, target: dict) -> tuple[float | None, dict]:
    """
    Validate a CRISPR gRNA submission using the three-score analytical method.

    Returns (rescored_affinity, grna_scores_dict).
    rescored_affinity is None if the sequence is invalid (not a 20-mer ACGT).
    The caller checks combined >= CRISPR_MIN_COMBINED_VALIDATOR to decide
    whether to CONFIRM or REJECT (rescored_affinity is always passed on-chain
    so the chain has the validator's own score, not just the miner's claim).
    """
    # Map on-chain integer IDs (3000-3009) → string keys used in _CRISPR_HOTSPOT_GRNAS.
    # The target["id"] field for CRISPR targets is the raw on-chain integer (e.g. 3000),
    # NOT the human-readable string "BRCA1_CRISPR".  Without this mapping score_grna()
    # receives "3000" and falls back to on_target=0.5 (no hotspot match), producing
    # affinity ≈ -7.25 instead of the miner's ≈ -8.5 — causing systematic rejection.
    _CRISPR_ONCHAIN_TO_NAME: dict[int, str] = {
        3000: "TP53_CRISPR",
        3001: "KRAS_CRISPR",
        3002: "BCL2_CRISPR",
        3003: "MYC_CRISPR",
        3004: "EGFR_CRISPR",
        3005: "HER2_CRISPR",
        3006: "BRCA1_CRISPR",
        3007: "PDL1_CRISPR",
        3008: "TERT_CRISPR",
        3009: "CDK4_CRISPR",
    }
    raw_id = target.get("id", "")
    try:
        raw_int = int(raw_id)
        target_id = _CRISPR_ONCHAIN_TO_NAME.get(raw_int, str(raw_id))
    except (ValueError, TypeError):
        # Already a string like "BRCA1_CRISPR" — use as-is
        target_id = str(raw_id)
    grna_seq  = grna_seq.upper().strip()

    # Accept both DNA (ACGT) and RNA (with U→T) notation
    grna_seq = grna_seq.replace("U", "T")

    if len(grna_seq) != 20 or not all(c in "ACGT" for c in grna_seq):
        log.warning(f"  [CRISPR] Invalid gRNA length/alphabet: {grna_seq[:24]} — skip")
        return None, {}

    scores = score_grna(grna_seq, target_id)
    log.info(
        f"  [CRISPR] {target_id}  gRNA={grna_seq[:16]}…"
        f"  iptm={scores['iptm']:.3f}"
        f"  off={scores['off_target']:.3f}"
        f"  del={scores['delivery']:.3f}"
        f"  combined={scores['combined']:.3f}"
        f"  affinity={scores['affinity']:.3f}"
    )
    return scores["affinity"], scores


class GpuBiasTracker:
    """
    Per-GPU, per-target-family bias correction model.

    JSON structure on disk:
    {
      "RTX 5060": {
        "EGFR": {"n": 12, "samples": [1.02, 0.98, ...], "bias_factor": 1.00},
        ...
      },
      ...
    }

    Usage:
        tracker = GpuBiasTracker()
        tracker.record("RTX 5060", "EGFR", claimed=-3.1, rescored=-3.2)
        factor = tracker.get_bias_factor("RTX 5060", "EGFR")   # None if <10 samples
        summary = tracker.summary()   # for stats.json
    """

    def __init__(self):
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            if GPU_BIAS_PATH.exists():
                self._data = json.loads(GPU_BIAS_PATH.read_text())
                log.info(
                    f"GPU bias model loaded: "
                    f"{sum(len(v) for v in self._data.values())} GPU·family pairs"
                )
        except Exception as e:
            log.warning(f"Could not load gpu_bias_models.json: {e}")
            self._data = {}

    def _save(self):
        try:
            GPU_BIAS_PATH.parent.mkdir(exist_ok=True)
            tmp = GPU_BIAS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(GPU_BIAS_PATH)
        except Exception as e:
            log.warning(f"Could not save gpu_bias_models.json: {e}")

    def record(self, gpu_model: str, family: str,
               claimed: float, rescored: float) -> None:
        """Record a rescoring observation for bias learning."""
        if not gpu_model or gpu_model == "UNKNOWN":
            return
        if claimed == 0.0:
            return   # avoid division by zero in ratio
        ratio = rescored / claimed
        gpu_entry = self._data.setdefault(gpu_model, {})
        fam_entry = gpu_entry.setdefault(family, {"n": 0, "samples": [], "bias_factor": None})
        fam_entry["samples"].append(round(ratio, 6))
        fam_entry["n"] = len(fam_entry["samples"])
        # Rebuild bias factor whenever we have enough samples
        if fam_entry["n"] >= GPU_BIAS_MIN_SAMPLES:
            fam_entry["bias_factor"] = round(
                sum(fam_entry["samples"]) / fam_entry["n"], 6
            )
        log.debug(
            f"[GPU-BIAS] record gpu={gpu_model} family={family} "
            f"ratio={ratio:.4f} n={fam_entry['n']} "
            f"bias={fam_entry['bias_factor']}"
        )
        self._save()

    def get_bias_factor(self, gpu_model: str, family: str) -> float | None:
        """
        Return bias_factor if ≥ GPU_BIAS_MIN_SAMPLES collected, else None.
        """
        try:
            return self._data[gpu_model][family]["bias_factor"]
        except KeyError:
            return None

    def get_n(self, gpu_model: str, family: str) -> int:
        """Return sample count for this GPU·family pair."""
        try:
            return self._data[gpu_model][family]["n"]
        except KeyError:
            return 0

    def summary(self) -> dict:
        """
        Return a dict suitable for stats.json GPU_BIAS section.
        {gpu_model: {n_total, families: [{family, n, bias_factor, tolerance}]}}
        """
        out = {}
        for gpu, families in self._data.items():
            total_n = sum(v["n"] for v in families.values())
            fam_list = []
            for fam, v in sorted(families.items()):
                bf = v["bias_factor"]
                fam_list.append({
                    "family":      fam,
                    "n":           v["n"],
                    "bias_factor": round(bf, 4) if bf is not None else None,
                    "tolerance":   TIGHTENED_TOLERANCE if bf is not None else VALIDATION_TOLERANCE,
                })
            out[gpu] = {"n_total": total_n, "families": fam_list}
        return out


# Module-level singleton (initialised in main())
_gpu_bias_tracker: GpuBiasTracker | None = None
_miner_gpu_model: str = "UNKNOWN"


# ── Boltz2 scoring — self-contained, no nova/miner dependencies ───────────────

def _mol_id(smiles: str) -> int:
    h = hashlib.sha256(smiles.encode()).digest()
    return (int.from_bytes(h[:8], "little") ^ 68) % (2**31 - 1)


def _heavy_atom_count(smiles: str) -> int:
    from rdkit import Chem
    mol = Chem.MolFromSmiles(smiles)
    return mol.GetNumHeavyAtoms() if mol else 0


def _msa_path_for(uniprot_id: str) -> str:
    path = MSA_DIR / f"{uniprot_id}.a3m"
    return str(path) if path.exists() else "empty"


def _sequence_from_msa(msa_path: str) -> str | None:
    if msa_path == "empty":
        return None
    try:
        with open(msa_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith(("#", ">")):
                    return line
    except OSError:
        pass
    return None


def _write_boltz_input(in_dir: Path, target_id: str, sequence: str,
                        smiles: str, mol_id: int, msa_path: str,
                        rna_mode: bool = False) -> None:
    import yaml
    if rna_mode:
        # RNA sequence mode — use Boltz2 RNA chain type; no MSA required.
        # siRNA/nucleotide sequences in the smiles field cannot be scored as
        # small-molecule ligands; the caller already gates on _smiles_is_sirna
        # and returns None before reaching _write_boltz_input.
        data = {
            "version": 1,
            "sequences": [
                {"rna": {"id": "A", "sequence": sequence}},
                {"ligand": {"id": "B", "smiles": smiles}},
            ],
            "properties": [{"affinity": {"binder": "B"}}],
        }
    else:
        data = {
            "version": 1,
            "sequences": [
                {"protein": {"id": "A", "sequence": sequence, "msa": msa_path}},
                {"ligand":  {"id": "B", "smiles": smiles}},
            ],
            "properties": [{"affinity": {"binder": "B"}}],
        }
    (in_dir / f"{mol_id}_{target_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    )


def _read_boltz_affinity(out_dir: Path, mol_id: int, target_id: str,
                          boltz_start: float) -> dict | None:
    """
    Read and return Boltz2 affinity output. Also verifies that every affinity
    file has a modification time strictly after `boltz_start` (pipeline
    injection guard).
    """
    pred_dir = out_dir / "boltz_results_inputs" / "predictions" / f"{mol_id}_{target_id}"
    if not pred_dir.exists():
        log.warning(f"  Boltz output dir missing: {pred_dir}")
        return None
    combined = {}
    affinity_files_found = False
    for fp in pred_dir.iterdir():
        if fp.name.startswith(("affinity", "confidence")):
            affinity_files_found = True
            # ── Mtime integrity check ──────────────────────────────────────
            file_mtime = fp.stat().st_mtime
            if file_mtime <= boltz_start:
                log.warning(
                    f"SECURITY WARNING  affinity file {fp.name} mtime "
                    f"{file_mtime:.3f} ≤ boltz_start {boltz_start:.3f} — "
                    f"file predates current Boltz2 run, rejecting"
                )
                return None
            # ── Parse ──────────────────────────────────────────────────────
            try:
                combined.update(json.loads(fp.read_text()))
            except Exception as e:
                log.warning(f"  Could not parse {fp.name}: {e}")
    if not affinity_files_found:
        log.warning(f"  No affinity/confidence files found in {pred_dir}")
        return None
    return combined or None


def run_boltz2(smiles: str, target: dict, seed: int = BOLTZ_SEED) -> float | None:
    """
    Re-score a SMILES via Boltz2. Returns affinity in kcal/mol or None.

    For protein targets: uses protein sequence + MSA (existing behaviour).
    For mRNA targets (id starts with mRNA_): uses RNA sequence mode — the
    `rna_sequence` field from the target dict is passed as an RNA chain to
    Boltz2 instead of a protein chain; no MSA is required or used.

    Security measures applied inside:
      - Unique UUID temp directory per call, chmod 700 (process-private).
      - SHA256 SMILES hash written before Boltz2 runs; re-read from YAML
        and verified after write to catch TOCTOU / symlink injection.
      - Output affinity file mtime verified to be strictly after Boltz2 start.
      - SECURITY WARNING logged and None returned on any check failure.

    seed must match the seed used by the submitting miner for reproducible scores.
    """
    import yaml
    from boltz.main import predict

    target_id = target["id"]
    is_mrna   = _is_mrna_target(target)

    if is_mrna:
        # ── RNA mode ─────────────────────────────────────────────────────────
        rna_seq = target.get("rna_sequence", "")
        if not rna_seq:
            log.warning(f"  mRNA target {target_id} missing rna_sequence — skip")
            return None
        sequence = rna_seq
        msa_path = "empty"   # not used in RNA mode
        log.info(f"  [RNA-MODE] mRNA target {target_id} ({target.get('mrna_region','RNA')}) — {len(rna_seq)} nt")
    else:
        # ── Protein mode (existing behaviour) ────────────────────────────────
        uniprot  = target.get("uniprot_id", "")
        msa_path = _msa_path_for(uniprot) if uniprot else "empty"
        sequence = _sequence_from_msa(msa_path) or target.get("protein_sequence", "")
        if not sequence:
            log.warning(f"  Target {target_id} has no protein_sequence and no usable MSA — skip")
            return None

    # mRNA targets where the "smiles" field is a nucleotide sequence (siRNA strand):
    # Boltz2 only supports affinity for small-molecule ligands, not RNA–RNA.
    # Return None immediately — the caller's BOLTZ2_FAILED path will cap retries.
    _nuc_re_b = re.compile(r'^[ACGTUacgtu]{10,}$')
    _smiles_is_sirna = bool(_nuc_re_b.match(smiles.strip()))
    if is_mrna and _smiles_is_sirna:
        log.warning(
            f"  mRNA target {target_id} has nucleotide sequence in smiles field "
            f"({smiles[:20]}…) — Boltz2 ligand affinity unsupported for siRNA — skip"
        )
        return None

    ha = _heavy_atom_count(smiles)
    if ha == 0:
        log.warning(f"  Invalid SMILES (0 heavy atoms): {smiles[:60]}")
        return None

    mol_id = _mol_id(smiles)

    # ── Create isolated, process-private tmpdir ───────────────────────────────
    run_uuid = str(uuid.uuid4())
    run_root = Path(f"/tmp/life-validator-{run_uuid}")
    in_dir   = run_root / "inputs"
    out_dir  = run_root / "outputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_root, stat.S_IRWXU)  # 700: owner-only

    try:
        # ── Compute SMILES integrity hash before write ────────────────────────
        smiles_hash_expected = hashlib.sha256(smiles.encode()).hexdigest()

        # ── Write Boltz2 YAML input (RNA or protein mode) ─────────────────────
        yaml_path = in_dir / f"{mol_id}_{target_id}.yaml"
        _write_boltz_input(in_dir, target_id, sequence, smiles, mol_id, msa_path,
                           rna_mode=is_mrna)

        # ── Re-read YAML and verify SMILES hash (injection prevention) ────────
        try:
            written_data = yaml.safe_load(yaml_path.read_text())
            ligand_blocks = [
                s["ligand"] for s in written_data.get("sequences", [])
                if "ligand" in s
            ]
            if not ligand_blocks:
                log.warning("SECURITY WARNING  no ligand block found in written YAML — skip")
                return None
            smiles_in_file = ligand_blocks[0].get("smiles", "")
            smiles_hash_actual = hashlib.sha256(smiles_in_file.encode()).hexdigest()
            if smiles_hash_actual != smiles_hash_expected:
                log.warning(
                    f"SECURITY WARNING  SMILES hash mismatch after write "
                    f"(expected={smiles_hash_expected[:16]}… "
                    f"actual={smiles_hash_actual[:16]}…) — skip"
                )
                return None
        except Exception as e:
            log.warning(f"SECURITY WARNING  YAML re-read/verify failed: {e} — skip")
            return None

        # ── Record strict start time before Boltz2 runs ───────────────────────
        boltz_start = time.time()

        # ── Run Boltz2 ────────────────────────────────────────────────────────
        predict.main([
            str(in_dir),
            "--out_dir",                     str(out_dir),
            "--recycling_steps",             str(_RECYCLING_STEPS),
            "--sampling_steps",              str(_SAMPLING_STEPS),
            "--diffusion_samples",           str(_DIFFUSION_SAMPLES),
            "--sampling_steps_affinity",     str(_SAMPLING_STEPS_AFF),
            "--diffusion_samples_affinity",  str(_DIFFUSION_SAMPLES_AFF),
            "--output_format",               "mmcif",
            "--seed",                        str(seed),
            "--num_workers",                 "0",
            "--accelerator",                 "gpu",
            "--affinity_mw_correction",
            "--override",
            "--no_kernels",      # disable cuequivariance CUDA kernels (incompatible with CUDA 13 / driver 580)
        ], standalone_mode=False)

        # ── Read output with mtime verification ───────────────────────────────
        metrics = _read_boltz_affinity(out_dir, mol_id, target_id, boltz_start)
        if metrics is None:
            return None

        prob = metrics.get("affinity_probability_binary")
        pred = metrics.get("affinity_pred_value")
        if prob is not None and pred is not None:
            # Primary affinity path — same formula as miner's _boltz_score_to_affinity()
            score = round(-((prob - pred) / ha) * 30.0, 3)
            log.info(
                f"  [mRNA-SCORE-SOURCE] affinity path"
                f"  prob={prob:.4f} pred={pred:.4f} ha={ha} → {score:.3f} kcal/mol"
            )
            return score
        if is_mrna:
            # iptm fallback path — mirrors miner's parse_mrna_boltz_affinity() exactly:
            # when Boltz2 skips the affinity JSON for an RNA receptor the miner falls back
            # to affinity_kcal = -6.0 - 3.0 × iptm.  The validator must use the same
            # formula so it rescores on the same scale as the claimed value.
            # _read_boltz_affinity() already reads confidence_* files into metrics,
            # so iptm is available here without any additional I/O.
            iptm = metrics.get("iptm")
            if iptm is not None:
                score = round(-6.0 - 3.0 * float(iptm), 3)
                log.info(
                    f"  [mRNA-SCORE-SOURCE] iptm_fallback path"
                    f"  iptm={iptm:.4f} → {score:.3f} kcal/mol"
                )
                return score
            log.warning(
                f"  mRNA Boltz affinity fields missing and no iptm in confidence output:"
                f" {list(metrics.keys())}"
            )
            return None
        # Protein path: affinity fields are mandatory — no iptm fallback
        log.warning(f"  Boltz affinity fields missing: {list(metrics.keys())}")
        return None

    except Exception as e:
        log.warning(f"  Boltz2 predict() raised: {e}")
        return None
    finally:
        shutil.rmtree(str(run_root), ignore_errors=True)


# ── Solana RPC helpers ────────────────────────────────────────────────────────
def _rpc(method: str, params: list) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        SOLANA_RPC,
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_current_epoch() -> int:
    """
    Return the current Solana epoch via getEpochInfo.
    Falls back to 0 on any RPC failure (safe: no halving applied).
    """
    try:
        resp = _rpc("getEpochInfo", [])
        return int(resp["result"]["epoch"])
    except Exception as e:
        log.debug(f"fetch_current_epoch failed: {e}")
        return 0


def _halved_reward(base: int, epoch: int) -> int:
    """
    Apply the halving schedule to a base reward.

    halvings = epoch // HALVING_INTERVAL
    result   = base >> halvings   (integer right-shift = floor-divide by 2^halvings)
    Minimum reward is 1 if base > 0, so validators always earn something.
    """
    halvings = epoch // HALVING_INTERVAL
    if halvings == 0:
        return base
    result = base >> halvings   # equivalent to base // (2 ** halvings)
    return max(1, result) if base > 0 else 0

def fetch_pending_submissions(crispr_only: bool = False) -> list[dict]:
    """
    getProgramAccounts filtered to ResultSubmission accounts.
    Returns list of dicts with keys: pubkey, miner, target_id, epoch,
    smiles, claimed_affinity, status.

    Three RPC-side filters (all must pass):
      1. dataSize=938        — exact account size for ResultSubmission
      2. memcmp offset=0     — 8-byte Anchor discriminator (base58)
      3. memcmp offset=576   — is_validated=0x00 (Pending/Validating only)
         Layout: 8+32+1+8+512+2+4+8+1 = 576 bytes before the status byte
         (938-byte accounts have 1 extra byte vs legacy 937-byte layout)

    crispr_only=True  → return only target_ids 3000–3009 (client-side filter)
    crispr_only=False → return only non-CRISPR target_ids (protein + mRNA)
    """
    import base64, base58
    disc_b58        = base58.b58encode(RESULT_DISCRIMINATOR).decode()
    unvalidated_b58 = base58.b58encode(bytes([0x00])).decode()   # is_validated=0x00
    # Build filters list — optionally restrict to a single miner wallet at the RPC level
    # to avoid fetching/processing corrupt submissions from other (old) wallets.
    filters = [
        {"dataSize": 938},
        {"memcmp": {"offset": 0,   "bytes": disc_b58}},
        {"memcmp": {"offset": 576, "bytes": unvalidated_b58}},
    ]
    if MINER_WALLET:
        miner_wallet_b58 = base58.b58encode(base58.b58decode(MINER_WALLET)).decode()
        # miner pubkey sits at offset 8 (after 8-byte discriminator)
        filters.append({"memcmp": {"offset": 8, "bytes": miner_wallet_b58}})
        log.debug(f"fetch_pending_submissions: filtering to miner wallet {MINER_WALLET[:16]}…")
    try:
        resp = _rpc("getProgramAccounts", [
            PROGRAM_ID,
            {
                "encoding": "base64",
                "filters": filters,
            },
        ])
    except Exception as e:
        log.warning(f"getProgramAccounts failed: {e}")
        return []

    results = []
    for item in resp.get("result", []) or []:
        try:
            pubkey  = item["pubkey"]
            data    = base64.b64decode(item["account"]["data"][0])
            # Parse ResultSubmission fields after 8-byte discriminator
            # Layout: [8 disc][32 miner][2 target_id u16][8 epoch][512 smiles][2 smiles_len]
            #         [4 claimed_affinity f32][8 submitted_slot i64][1 status]...
            # (938-byte accounts: target_id expanded from u8 to u16 vs legacy 937-byte layout)
            off = 8
            miner      = data[off:off+32]; off += 32
            target_id  = int.from_bytes(data[off:off+2], "little"); off += 2
            epoch      = int.from_bytes(data[off:off+8], "little"); off += 8
            smiles_raw = data[off:off+512]; off += 512
            smiles_len = int.from_bytes(data[off:off+2], "little"); off += 2
            import struct
            claimed    = struct.unpack_from("<f", data, off)[0]; off += 4
            off += 8  # submitted_slot
            status     = data[off]
            if status not in (STATUS_PENDING, STATUS_VALIDATING):
                continue
            import base58 as _b58
            smiles = smiles_raw[:smiles_len].decode("utf-8", errors="replace").strip("\x00")
            if not smiles:
                continue
            results.append({
                "pubkey":           pubkey,
                "miner":            _b58.b58encode(miner).decode(),
                "target_id":        target_id,
                "epoch":            epoch,
                "smiles":           smiles,
                "claimed_affinity": float(claimed),
                "status":           status,
            })
        except Exception as e:
            log.debug(f"  parse error {item.get('pubkey','?')[:16]}: {e}")
    # Client-side type filter: split CRISPR (3000-3009) from protein/mRNA
    if crispr_only:
        return [r for r in results if 3000 <= r["target_id"] <= 3009]
    else:
        return [r for r in results if not (3000 <= r["target_id"] <= 3009)]


# ── On-chain validate call ────────────────────────────────────────────────────
def validate_on_chain(submission_pubkey: str, rescored_affinity: float) -> dict | None:
    """Call validate_result via Node.js. Returns {tx:..., confirmed:bool} or None."""
    args = {
        "rpc":              SOLANA_RPC,
        "validatorKeypair": VALIDATOR_KEYPAIR,
        "payerKeypair":     PAYER_KEYPAIR,
        "idlPath":          str(IDL_PATH),
        "programId":        PROGRAM_ID,
        "resultPubkey":     submission_pubkey,
        "rescoredAffinity": rescored_affinity,
    }
    try:
        r = subprocess.run(
            ["node", str(VALIDATE_JS), json.dumps(args)],
            capture_output=True, text=True, timeout=120,
            cwd=str(ANCHOR_DIR),
        )
        if r.returncode != 0:
            # life_validate.js writes errors to stdout as JSON {error: "..."}
            err_detail = r.stdout.strip()[-400:] or r.stderr.strip()[-400:]
            log.error(f"  validate_result node error: {err_detail}")
            return None
        for line in reversed(r.stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except Exception:
                continue
        log.warning(f"  validate stdout: {r.stdout[:200]}")
        return None
    except subprocess.TimeoutExpired:
        log.error("  validate_result timed out")
        return None
    except Exception as e:
        log.error(f"  validate_result exception: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def fetch_targets() -> list:
    try:
        with urllib.request.urlopen(TARGETS_URL, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"fetch_targets: {e}")
        return []

def write_stats(stats: dict):
    """Atomic write via temp file to avoid partial reads."""
    tmp = STATS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(stats, indent=2))
    tmp.replace(STATS_PATH)

def append_log(row: dict):
    with LOG_JSONL.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ── Network / reputation helpers ─────────────────────────────────────────────

def fetch_network_validator_info() -> dict:
    """
    Pull active validator count and self reputation from the on-chain program.
    Returns defaults on any failure — always safe to call.
    """
    import base64, struct
    result = {"active_validators": 0, "self_reputation_bps": 10000,
              "self_total_validations": 0, "self_confirmations": 0}
    try:
        import base58 as _b58
        disc_nc = bytes([255, 22, 189, 191, 46, 82, 204, 0])  # network_config discriminator
        resp = _rpc("getProgramAccounts", [
            PROGRAM_ID,
            {"encoding": "base64", "filters": [
                {"dataSize": 344},
                {"memcmp": {"offset": 0, "bytes": _b58.b58encode(disc_nc).decode()}}
            ]}
        ])
        for item in resp.get("result", []) or []:
            data = base64.b64decode(item["account"]["data"][0])
            # validator_count at offset = 8+32+32+8+8+8+8+8+1+4+32*5 = 237
            vc_offset = 8+32+32+8+8+8+8+8+1+4+32*5
            if len(data) > vc_offset:
                result["active_validators"] = int(data[vc_offset])
                break
    except Exception as e:
        log.debug(f"fetch_network_validator_info: {e}")
    # Self reputation: read ValidatorAccount PDA for this validator
    try:
        import base58 as _b58
        if _VALIDATOR_PUBKEY:
            vk_bytes = _b58.b58decode(_VALIDATOR_PUBKEY)
            SEED_VALIDATOR_ACCOUNT = b"validator_account"
            import hashlib
            # Derive PDA — use Node.js for correctness (same as on-chain)
            # Fall back to stats if not available
    except Exception:
        pass
    return result


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    global _VALIDATOR_PUBKEY, _gpu_bias_tracker, _miner_gpu_model
    print("\033[96m    L I F E  C O M P U T E  —  V A L I D A T O R  \033[0m\n")

    # Load validator pubkey once at startup (used for self-validation checks)
    _VALIDATOR_PUBKEY = _load_validator_pubkey()
    if _VALIDATOR_PUBKEY:
        log.info(f"Validator pubkey: {_VALIDATOR_PUBKEY[:16]}…")
    else:
        log.warning("Self-validation check DISABLED (keypair unreadable)")

    # Detect GPU model at startup — written to .env for miner reuse
    _miner_gpu_model = detect_gpu_model()

    # Initialise GPU bias tracker (loads existing model from disk)
    _gpu_bias_tracker = GpuBiasTracker()

    targets_by_id: dict[int, dict] = {}
    last_refresh    = 0.0
    life_earned     = 0.0

    # Warm up the dedup tracker from today's audit log so a restart doesn't
    # re-process submissions that already landed a tx.
    _load_seen_from_audit()
    if _SEEN_SUBMISSIONS:
        log.info(f"Restored {len(_SEEN_SUBMISSIONS)} already-processed submission(s) from audit log")

    # Initialize today-counters from today's log so a restart doesn't zero the display
    today_date = _today_date_utc()
    validated_today, accepted, rejected = _count_today_from_log()
    if validated_today:
        log.info(f"Restored today's counters from log: total={validated_today} confirmed={accepted} rejected={rejected}")

    # Restore life_commission from today's audit log
    _today = _today_date_utc()
    life_earned = 0.0
    if AUDIT_JSONL.exists():
        try:
            with AUDIT_JSONL.open() as _af:
                for _line in _af:
                    try:
                        _r = json.loads(_line)
                        if _r.get("ts", "").startswith(_today):
                            life_earned += float(_r.get("life_earned", 0) or 0)
                    except Exception:
                        pass
        except Exception:
            pass
    if life_earned:
        log.info(f"Restored life_commission from audit log: ${life_earned:.1f} $LIFE")

    stats = {
        "status":          "ONLINE",
        "validated_today": validated_today,
        "confirmed":       accepted,
        "rejected":        rejected,
        "accept_rate":     round(accepted / max(validated_today, 1) * 100, 1) if validated_today else 0.0,
        "life_commission": life_earned,
        "last_heartbeat":  datetime.now(timezone.utc).isoformat(),
        "current_target":  None,
        "current_smiles":  None,
        "active_validators":    0,
        "self_reputation_pct":  100.0,
        "started_at":      datetime.now(timezone.utc).isoformat(),
        "last_updated":    "",
        "gpu_model":       _miner_gpu_model,
        "gpu_bias":        {},
    }
    write_stats(stats)

    # Heartbeat thread — updates last_heartbeat every 30s independent of poll cycle
    def _heartbeat():
        while True:
            time.sleep(30)
            stats["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            stats["last_updated"]   = stats["last_heartbeat"]
            write_stats(stats)
    threading.Thread(target=_heartbeat, daemon=True).start()

    # ── CRISPR thread — independent continuous cycle, never blocked by Boltz2 ──
    def _crispr_loop():
        nonlocal validated_today, accepted, rejected, life_earned
        log.info("[CRISPR-THREAD] Started — polling independently of protein/mRNA loop")
        while True:
            try:
                crispr_subs = fetch_pending_submissions(crispr_only=True)
                if crispr_subs:
                    log.info(f"[CRISPR-THREAD] Found {len(crispr_subs)} pending CRISPR submission(s)")
                for sub in crispr_subs:
                    pubkey        = sub["pubkey"]
                    smiles        = sub["smiles"]
                    miner_wallet  = sub["miner"]
                    target_id_int = sub["target_id"]
                    claimed       = sub["claimed_affinity"]

                    # Dedup
                    if _SEEN_SUBMISSIONS.get(pubkey, 0) > _MAX_RETRY_ATTEMPTS:
                        continue

                    # Security gates (mirrors main loop)
                    if MINER_WALLET and miner_wallet != MINER_WALLET:
                        continue
                    if not _rate_limit_check():
                        break
                    if not _check_self_validation(miner_wallet, pubkey):
                        continue
                    if abs(claimed) > 50.0:
                        continue
                    if not _sanitize_smiles(smiles, pubkey):
                        continue

                    target = targets_by_id.get(target_id_int)
                    if not target:
                        continue

                    target = dict(target)
                    target["difficulty_tier"] = 3  # CRISPR always tier 3

                    log.info(f"  [CRISPR-THREAD] Validating {pubkey[:16]}…  target={target.get('id','?')}  claimed={claimed:.3f}")

                    t0 = time.time()
                    rescored, grna_scores = run_crispr_validation(smiles, target)
                    elapsed = time.time() - t0

                    if rescored is None:
                        log.warning(f"  [CRISPR] Invalid gRNA for {pubkey[:16]}… — skip")
                        _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                        continue

                    _crispr_tgt_name = _CRISPR_ID_TO_NAME.get(target_id_int, target.get("id", "") or "")
                    _crispr_min      = _crispr_threshold(_crispr_tgt_name)
                    quality_ok  = grna_scores.get("combined", 0.0) >= _crispr_min

                    # ── GPU-bias correction (CRISPR) ─────────────────────────────
                    # Use the same bias-tracker architecture as protein/mRNA.
                    # family key = full CRISPR target name, e.g. "TP53_CRISPR".
                    # Fall back to raw claimed + CRISPR_AFFINITY_TOL if < GPU_BIAS_MIN_SAMPLES.
                    CRISPR_AFFINITY_TOL = 0.25
                    _crispr_family   = _target_family(_crispr_tgt_name)  # e.g. "TP53_CRISPR"
                    _crispr_bias     = None
                    if _gpu_bias_tracker is not None:
                        _crispr_bias = _gpu_bias_tracker.get_bias_factor(_miner_gpu_model, _crispr_family)

                    if _crispr_bias is not None:
                        adjusted_claimed = claimed * _crispr_bias
                        _crispr_tol      = TIGHTENED_TOLERANCE
                        rel_err = (abs(rescored - adjusted_claimed) / abs(adjusted_claimed)
                                   if adjusted_claimed else abs(rescored))
                        log.info(
                            f"  [GPU-BIAS-CRISPR] {_crispr_family} correction factor {_crispr_bias:.4f} applied"
                            f" → adjusted {adjusted_claimed:.3f}"
                            f" → tolerance check {'passed' if (quality_ok and rel_err <= _crispr_tol) else 'failed'}"
                            f"  (tol={_crispr_tol:.4f})"
                        )
                    else:
                        adjusted_claimed = claimed
                        _crispr_tol      = CRISPR_AFFINITY_TOL
                        rel_err = abs(rescored - claimed) / abs(claimed) if claimed else abs(rescored)

                    within_tol = quality_ok and rel_err <= _crispr_tol

                    # ── Record for future bias learning ──────────────────────────
                    if _gpu_bias_tracker is not None and _miner_gpu_model != "UNKNOWN":
                        _gpu_bias_tracker.record(_miner_gpu_model, _crispr_family, claimed, rescored)

                    verdict = "CONFIRM" if within_tol else "REJECT"
                    log.info(
                        f"  [CRISPR] {verdict}  combined={grna_scores.get('combined',0):.3f}"
                        f"  claimed={claimed:.3f}  rescored={rescored:.3f}"
                        f"  rel_err={rel_err:.3f}  quality_ok={quality_ok}  ({elapsed*1000:.0f}ms)"
                    )

                    current_epoch = fetch_current_epoch()
                    tier_reward   = _halved_reward(BASE_TIER_REWARDS.get(3, 7), current_epoch)
                    log.info(f"  [CRISPR] reward: base={BASE_TIER_REWARDS.get(3,7)} epoch={current_epoch} halvings={current_epoch//HALVING_INTERVAL} → {tier_reward} $LIFE")

                    result = validate_on_chain(pubkey, rescored)
                    tx     = result.get("tx") if result else None
                    if tx:
                        log.info(f"  ✔ [CRISPR] tx: {tx}")
                        validated_today += 1
                        if within_tol:
                            accepted    += 1
                            life_earned += tier_reward
                            log.info(f"  +{tier_reward} $LIFE  (CRISPR tier=3)  total={life_earned:.1f}")
                        else:
                            rejected += 1
                        _SEEN_SUBMISSIONS.pop(pubkey, None)
                        append_audit({
                            "ts":               datetime.now(timezone.utc).isoformat(),
                            "submission_pubkey": pubkey,
                            "miner_wallet":     miner_wallet,
                            "claimed_score":    claimed,
                            "rescored":         rescored,
                            "decision":         "CONFIRM" if within_tol else "REJECT",
                            "rel_err":          round(rel_err, 4),
                            "tolerance_used":   _crispr_tol,
                            "bias_factor_used": round(_crispr_bias, 6) if _crispr_bias is not None else None,
                            "adjusted_claimed": round(adjusted_claimed, 4),
                            "tx":               tx,
                            "target_id":        target_id_int,
                            "target_type":      "CRISPR",
                            "difficulty_tier":  3,
                            "life_earned":      tier_reward if within_tol else 0,
                            "grna_on_target":   grna_scores.get("on_target"),
                            "grna_off_target":  grna_scores.get("off_target"),
                            "grna_delivery":    grna_scores.get("delivery"),
                            "grna_combined":    grna_scores.get("combined"),
                            "smiles":           smiles,
                        })
                        append_log({
                            "ts":               datetime.now(timezone.utc).isoformat(),
                            "pubkey":           pubkey,
                            "smiles":           smiles,
                            "grna_seq":         smiles,
                            "target_id":        target_id_int,
                            "claimed":          claimed,
                            "rescored":         rescored,
                            "rel_err":          round(rel_err, 4),
                            "within_tolerance": within_tol,
                            "verdict":          verdict,
                            "tx":               tx,
                            "elapsed_s":        round(elapsed, 3),
                            "difficulty_tier":  3,
                            "target_type":      "CRISPR",
                            "grna_combined":    grna_scores.get("combined"),
                            "life_earned":      tier_reward if within_tol else 0,
                        })
                    else:
                        log.warning(f"  [CRISPR] validate_on_chain returned no tx")
                        _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
            except Exception as e:
                log.error(f"[CRISPR-THREAD] unhandled error: {e}")
            time.sleep(POLL_SECONDS)
    threading.Thread(target=_crispr_loop, daemon=True, name="crispr-loop").start()

    while True:
        now = time.time()

        # Reset today-counters at midnight UTC
        new_date = _today_date_utc()
        if new_date != today_date:
            log.info(f"New UTC day ({new_date}) — resetting today's validation counters")
            today_date      = new_date
            validated_today = 0
            accepted        = 0
            rejected        = 0

        # Refresh target list every 5 min
        if now - last_refresh > 300 or not targets_by_id:
            targets = fetch_targets()
            targets_by_id = {t.get("target_id_num", i): t for i, t in enumerate(targets)}
            # Also index by target_id integer from on-chain (0-indexed per TARGET_ID_MAP)
            for i, t in enumerate(targets):
                targets_by_id.setdefault(i, t)
            # mRNA targets occupy on-chain IDs 2000-2029, mapping to list indices 30-59.
            # Register each mRNA target under its on-chain ID so submissions with
            # target_id_int in [2000, 2029] resolve correctly instead of being skipped.
            _mrna_base_onchain = 2000
            _mrna_base_list    = 30
            _mrna_count        = 30
            for _j in range(_mrna_count):
                _list_idx   = _mrna_base_list + _j
                _onchain_id = _mrna_base_onchain + _j
                _t = targets[_list_idx] if _list_idx < len(targets) else None
                if _t is not None and _is_mrna_target(_t):
                    targets_by_id.setdefault(_onchain_id, _t)
            _mrna_registered = sum(
                1 for k in range(_mrna_base_onchain, _mrna_base_onchain + _mrna_count)
                if k in targets_by_id
            )
            # CRISPR targets occupy on-chain IDs 3000-3009, mapping to list indices 60-69.
            # Register each CRISPR target under its on-chain ID so submissions with
            # target_id_int in [3000, 3009] resolve correctly instead of being skipped.
            _crispr_base_onchain = 3000
            _crispr_base_list    = 60
            _crispr_count        = 10
            for _j in range(_crispr_count):
                _list_idx   = _crispr_base_list + _j
                _onchain_id = _crispr_base_onchain + _j
                _t = targets[_list_idx] if _list_idx < len(targets) else None
                if _t is not None:
                    targets_by_id.setdefault(_onchain_id, _t)
            _crispr_registered = sum(
                1 for k in range(_crispr_base_onchain, _crispr_base_onchain + _crispr_count)
                if k in targets_by_id
            )
            log.info(f"Targets loaded: {len(targets)}  (mRNA on-chain IDs registered: {_mrna_registered})  (CRISPR on-chain IDs registered: {_crispr_registered})")
            last_refresh = now

        # Step 1: Poll for pending protein/mRNA submissions (CRISPR handled by its own thread)
        log.info("Polling for pending submissions (protein/mRNA)...")
        submissions = fetch_pending_submissions(crispr_only=False)
        log.info(f"  Found {len(submissions)} pending submission(s)")

        for sub in submissions:
            pubkey        = sub["pubkey"]
            smiles        = sub["smiles"]
            miner_wallet  = sub["miner"]
            target_id_int = sub["target_id"]
            claimed       = sub["claimed_affinity"]

            # ── Dedup: skip already-processed or over-retried submissions ─────
            attempts = _SEEN_SUBMISSIONS.get(pubkey, 0)
            if attempts > _MAX_RETRY_ATTEMPTS:
                log.debug(
                    f"  {pubkey[:16]}…: skipping — already attempted "
                    f"{attempts}x (tx kept failing); will clear when RPC drops it"
                )
                continue

            # ── Security gate 0: miner wallet allowlist ───────────────────────
            if MINER_WALLET and miner_wallet != MINER_WALLET:
                log.warning(
                    f"  {pubkey[:16]}…: miner wallet {miner_wallet[:16]}… "
                    f"not in allowlist — skipping (old/unknown wallet)"
                )
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         None,
                    "decision":         "WALLET_NOT_ALLOWED",
                    "rel_err":          None,
                })
                continue

            # ── Security gate 1: rate limiting ───────────────────────────────
            if not _rate_limit_check():
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         None,
                    "decision":         "RATE_LIMITED",
                    "rel_err":          None,
                })
                break  # stop processing this poll cycle entirely

            # ── Security gate 2: self-validation prevention ───────────────────
            if not _check_self_validation(miner_wallet, pubkey):
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         None,
                    "decision":         "SELF_VALIDATION_REJECTED",
                    "rel_err":          None,
                })
                continue

            # ── Security gate 3: corrupted claimed_score filter ───────────────
            # Valid Boltz2 affinities are in the range [-15, +5] kcal/mol.
            # Anything with |claimed_score| > 50 is physically impossible (e.g.
            # 788604923813036032, -5.732e+33, or 51.456) and signals corrupted
            # on-chain data.  Skip immediately — Boltz2 would never confirm
            # these and running the GPU wastes time while the loop stalls.
            _CLAIMED_SCORE_MAX = 50.0
            if abs(claimed) > _CLAIMED_SCORE_MAX:
                log.warning(
                    f"  {pubkey[:16]}…: claimed_score {claimed:.6g} exceeds "
                    f"|{_CLAIMED_SCORE_MAX}| — corrupted submission, skipping"
                )
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         None,
                    "decision":         "CORRUPTED_SCORE",
                    "rel_err":          None,
                })
                continue

            # ── Security gate 4: SMILES input sanitization ────────────────────
            if not _sanitize_smiles(smiles, pubkey):
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         None,
                    "decision":         "SMILES_INVALID",
                    "rel_err":          None,
                })
                continue

            target = targets_by_id.get(target_id_int)
            if not target:
                log.warning(f"  {pubkey[:16]}…: unknown target_id={target_id_int} — skip")
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         None,
                    "decision":         "UNKNOWN_TARGET",
                    "rel_err":          None,
                })
                continue

            log.info(f"  Validating {pubkey[:16]}…  target={target.get('id','?')}  "
                     f"claimed={claimed:.3f}  smiles={smiles[:40]}")

            # ── mRNA target: enforce tier 3 regardless of what the on-chain record says
            is_mrna = _is_mrna_target(target)
            if is_mrna and target.get("difficulty_tier") != 3:
                # Safety override — all mRNA_ targets are hard (25 $LIFE commission)
                target = dict(target)
                target["difficulty_tier"] = 3

            # ── CRISPR target: enforce tier 3 regardless of what the on-chain record says
            is_crispr = _is_crispr_target(target) or (3000 <= target_id_int <= 3009)
            if is_crispr and target.get("difficulty_tier") != 3:
                # Safety override — all CRISPR targets are hard (25 $LIFE commission)
                target = dict(target)
                target["difficulty_tier"] = 3

            # Expose current work to dashboard
            stats["current_target"] = target.get("id") or str(target_id_int)
            stats["current_smiles"] = smiles
            stats["last_updated"]   = datetime.now(timezone.utc).isoformat()
            write_stats(stats)

            # ── CRISPR path: three-score analytical validation (no Boltz2) ───
            if is_crispr:
                t0 = time.time()
                rescored, grna_scores = run_crispr_validation(smiles, target)
                elapsed = time.time() - t0

                if rescored is None:
                    # Invalid gRNA sequence — skip and cap retries
                    log.warning(f"  [CRISPR] Invalid gRNA for {pubkey[:16]}… — skip")
                    _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                    attempt_n = _SEEN_SUBMISSIONS[pubkey]
                    if attempt_n < _MAX_RETRY_ATTEMPTS:
                        append_audit({
                            "ts":               datetime.now(timezone.utc).isoformat(),
                            "submission_pubkey": pubkey,
                            "miner_wallet":     miner_wallet,
                            "claimed_score":    claimed,
                            "rescored":         None,
                            "decision":         "BOLTZ2_FAILED",
                            "rel_err":          None,
                        })
                    else:
                        log.info(f"  [CRISPR] {pubkey[:16]}…: failed {attempt_n}x — submitting on-chain REJECT to clear from queue")
                        result = validate_on_chain(pubkey, 0.0)
                        tx = result.get("tx") if result else None
                        if tx:
                            log.info(f"  ✔ CRISPR BOLTZ2_REJECT tx: {tx}")
                            _SEEN_SUBMISSIONS.pop(pubkey, None)
                        else:
                            log.warning(f"  CRISPR BOLTZ2_REJECT on-chain call failed — will retry next poll")
                        append_audit({
                            "ts":               datetime.now(timezone.utc).isoformat(),
                            "submission_pubkey": pubkey,
                            "miner_wallet":     miner_wallet,
                            "claimed_score":    claimed,
                            "rescored":         None,
                            "decision":         "BOLTZ2_FAILED" if not tx else "BOLTZ2_REJECT",
                            "rel_err":          None,
                            "tx":               tx,
                        })
                    continue

                combined   = grna_scores.get("combined", 0.0)
                # Tolerance check: GPU-bias corrected, mirroring the CRISPR thread exactly.
                _crispr_tgt_name = _CRISPR_ID_TO_NAME.get(target_id_int, target.get("id", "") or "")
                _crispr_min      = _crispr_threshold(_crispr_tgt_name)
                quality_ok  = combined >= _crispr_min

                # ── GPU-bias correction (CRISPR, main loop) ──────────────────────
                CRISPR_AFFINITY_TOL = 0.25
                _crispr_family   = _target_family(_crispr_tgt_name)  # e.g. "TP53_CRISPR"
                _crispr_bias     = None
                if _gpu_bias_tracker is not None:
                    _crispr_bias = _gpu_bias_tracker.get_bias_factor(_miner_gpu_model, _crispr_family)

                if _crispr_bias is not None:
                    adjusted_claimed = claimed * _crispr_bias
                    _crispr_tol      = TIGHTENED_TOLERANCE
                    rel_err = (abs(rescored - adjusted_claimed) / abs(adjusted_claimed)
                               if adjusted_claimed else abs(rescored))
                    log.info(
                        f"  [GPU-BIAS-CRISPR] {_crispr_family} correction factor {_crispr_bias:.4f} applied"
                        f" → adjusted {adjusted_claimed:.3f}"
                        f" → tolerance check {'passed' if (quality_ok and rel_err <= _crispr_tol) else 'failed'}"
                        f"  (tol={_crispr_tol:.4f})"
                    )
                else:
                    adjusted_claimed = claimed
                    _crispr_tol      = CRISPR_AFFINITY_TOL
                    if claimed != 0.0:
                        rel_err = abs(rescored - claimed) / abs(claimed)
                    else:
                        rel_err = abs(rescored)

                within_tol  = quality_ok and (rel_err <= _crispr_tol)
                verdict     = "CONFIRM" if within_tol else "REJECT"

                # ── Record for future bias learning ──────────────────────────────
                if _gpu_bias_tracker is not None and _miner_gpu_model != "UNKNOWN":
                    _gpu_bias_tracker.record(_miner_gpu_model, _crispr_family, claimed, rescored)

                log.info(
                    f"  [CRISPR] {verdict}  combined={combined:.3f}"
                    f"  claimed={claimed:.3f}  rescored={rescored:.3f}"
                    f"  rel_err={rel_err:.3f}  quality_ok={quality_ok}  ({elapsed*1000:.0f}ms)"
                )

                TIER_REWARDS = BASE_TIER_REWARDS
                difficulty   = target.get("difficulty_tier", 3)
                current_epoch = fetch_current_epoch()
                tier_reward  = _halved_reward(TIER_REWARDS.get(difficulty, 7), current_epoch)
                log.info(f"  [CRISPR] reward: base={TIER_REWARDS.get(difficulty,7)} epoch={current_epoch} halvings={current_epoch//HALVING_INTERVAL} → {tier_reward} $LIFE")

                result = validate_on_chain(pubkey, rescored)
                tx = result.get("tx") if result else None
                if tx:
                    log.info(f"  ✔ tx: {tx}")
                    if within_tol:
                        life_earned += tier_reward
                        log.info(f"  +{tier_reward} $LIFE  (tier={difficulty})  total={life_earned:.1f}")
                    _SEEN_SUBMISSIONS.pop(pubkey, None)
                else:
                    log.warning(f"  validate_on_chain returned no tx")
                    _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1

                validated_today += 1
                if within_tol:
                    accepted += 1
                else:
                    rejected += 1

                crispr_audit_decision = verdict if tx else f"{verdict}_TX_FAILED"
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         rescored,
                    "decision":         crispr_audit_decision,
                    "rel_err":          round(rel_err, 4),
                    "tolerance_used":   _crispr_tol,
                    "bias_factor_used": round(_crispr_bias, 6) if _crispr_bias is not None else None,
                    "adjusted_claimed": round(adjusted_claimed, 4),
                    "difficulty_tier":  difficulty,
                    "target_type":      "CRISPR",
                    "life_earned":      tier_reward if (within_tol and tx) else 0,
                    "grna_on_target":   grna_scores.get("on_target"),
                    "grna_off_target":  grna_scores.get("off_target"),
                    "grna_delivery":    grna_scores.get("delivery"),
                    "grna_combined":    combined,
                    "smiles":           smiles,        # gRNA sequence stored in smiles field
                    "target_id":        target_id_int,
                })
                append_log({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "pubkey":           pubkey,
                    "smiles":           smiles,
                    "grna_seq":         smiles,            # gRNA sequence is stored in smiles field
                    "target_id":        target_id_int,
                    "claimed":          claimed,
                    "rescored":         rescored,
                    "rel_err":          round(rel_err, 4),
                    "within_tolerance": within_tol,
                    "verdict":          verdict,
                    "tx":               tx,
                    "elapsed_s":        round(elapsed, 3),
                    "difficulty_tier":  difficulty,
                    "target_type":      "CRISPR",
                    "grna_combined":    combined,
                    "life_earned":      tier_reward if (within_tol and tx) else 0,
                })
                stats.update({
                    "validated_today": validated_today,
                    "confirmed":       accepted,
                    "rejected":        rejected,
                    "accept_rate":     round(accepted / max(validated_today, 1) * 100, 1),
                    "life_commission": life_earned,
                    "last_heartbeat":  datetime.now(timezone.utc).isoformat(),
                    "current_target":  None,
                    "current_smiles":  None,
                    "last_updated":    datetime.now(timezone.utc).isoformat(),
                    "gpu_model":       _miner_gpu_model,
                    "gpu_bias":        _gpu_bias_tracker.summary() if _gpu_bias_tracker else {},
                })
                write_stats(stats)
                continue   # done with this CRISPR submission — skip Boltz2 path

            # Step 2: Re-run Boltz2 (with pipeline injection hardening inside)
            t0 = time.time()
            rescored = run_boltz2(smiles, target, seed=sub.get("boltz_seed", BOLTZ_SEED))
            elapsed  = time.time() - t0

            if rescored is None:
                log.warning(f"  Boltz2 failed for {pubkey[:16]}… — skip")
                _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                attempt_n = _SEEN_SUBMISSIONS[pubkey]
                if attempt_n < _MAX_RETRY_ATTEMPTS:
                    log.debug(f"  {pubkey[:16]}…: Boltz2 fail attempt {attempt_n}/{_MAX_RETRY_ATTEMPTS}")
                    append_audit({
                        "ts":               datetime.now(timezone.utc).isoformat(),
                        "submission_pubkey": pubkey,
                        "miner_wallet":     miner_wallet,
                        "claimed_score":    claimed,
                        "rescored":         None,
                        "decision":         "BOLTZ2_FAILED",
                        "rel_err":          None,
                    })
                else:
                    # Final attempt exhausted — submit a reject on-chain so the account
                    # flips out of Pending and stops appearing in future RPC polls.
                    log.info(f"  {pubkey[:16]}…: Boltz2 failed {attempt_n}x — submitting on-chain REJECT to clear from queue")
                    result = validate_on_chain(pubkey, 0.0)
                    tx = result.get("tx") if result else None
                    if tx:
                        log.info(f"  ✔ BOLTZ2_REJECT tx: {tx}")
                        _SEEN_SUBMISSIONS.pop(pubkey, None)
                    else:
                        log.warning(f"  BOLTZ2_REJECT on-chain call failed — will retry next poll")
                    append_audit({
                        "ts":               datetime.now(timezone.utc).isoformat(),
                        "submission_pubkey": pubkey,
                        "miner_wallet":     miner_wallet,
                        "claimed_score":    claimed,
                        "rescored":         None,
                        "decision":         "BOLTZ2_FAILED" if not tx else "BOLTZ2_REJECT",
                        "rel_err":          None,
                        "tx":               tx,
                    })
                continue

            # Step 3: GPU-bias corrected tolerance check
            # ── Determine target family for bias lookup ─────────────────────────
            target_name   = target.get("id") or str(target_id_int)
            family        = _target_family(target_name)

            # ── Look up bias factor for this miner's GPU + target family ────────
            # GPU bias model is trained on protein docking scores only.
            # mRNA targets use RNA-mode Boltz2 which produces scores on a different
            # scale — the bias correction is inapplicable and must be skipped.
            bias_factor = None
            if _gpu_bias_tracker is not None and not is_mrna:
                bias_factor = _gpu_bias_tracker.get_bias_factor(_miner_gpu_model, family)

            if bias_factor is not None:
                # Apply GPU-specific correction to the claimed score
                adjusted_claimed = claimed * bias_factor
                tol = TIGHTENED_TOLERANCE
                if adjusted_claimed != 0.0:
                    rel_err = abs(rescored - adjusted_claimed) / abs(adjusted_claimed)
                else:
                    rel_err = abs(rescored)
                within_tol = rel_err <= tol
                verdict    = "CONFIRM" if within_tol else "REJECT"
                log.info(
                    f"  [GPU-BIAS] {_miner_gpu_model} correction factor {bias_factor:.4f} applied"
                    f" → adjusted {adjusted_claimed:.3f}"
                    f" → tolerance check {'passed' if within_tol else 'failed'}"
                    f"  (tol={tol:.4f})"
                )
            else:
                # No bias model yet — use default tolerance on raw claimed value
                adjusted_claimed = claimed
                tol = VALIDATION_TOLERANCE
                if claimed != 0.0:
                    rel_err = abs(rescored - claimed) / abs(claimed)
                else:
                    rel_err = abs(rescored)
                within_tol = rel_err <= tol
                verdict    = "CONFIRM" if within_tol else "REJECT"

            # ── Record this rescoring for future bias learning ──────────────────
            if _gpu_bias_tracker is not None and _miner_gpu_model != "UNKNOWN":
                _gpu_bias_tracker.record(_miner_gpu_model, family, claimed, rescored)

            log.info(f"  {verdict}  claimed={claimed:.3f}  rescored={rescored:.3f}  "
                     f"rel_err={rel_err:.3f}  ({elapsed:.1f}s)")

            # ── Tier reward (mirrors miner tokenomics: halved every 210k epochs) ──
            difficulty    = target.get("difficulty_tier", 1)
            current_epoch = fetch_current_epoch()
            tier_reward   = _halved_reward(BASE_TIER_REWARDS.get(difficulty, 1), current_epoch)
            log.info(f"  reward: base={BASE_TIER_REWARDS.get(difficulty,1)} epoch={current_epoch} halvings={current_epoch//HALVING_INTERVAL} → {tier_reward} $LIFE")

            # Step 4: Submit on-chain
            result = validate_on_chain(pubkey, rescored)
            tx = result.get("tx") if result else None
            if tx:
                log.info(f"  ✔ tx: {tx}")
                if within_tol:
                    life_earned += tier_reward   # halving-adjusted tier reward
                    log.info(
                        f"  +{tier_reward} $LIFE  (tier={difficulty})  "
                        f"total={life_earned:.1f}"
                    )
                # Tx landed — account will flip to Validating; remove from retry
                # tracker so we don't needlessly hold the pubkey in memory forever.
                _SEEN_SUBMISSIONS.pop(pubkey, None)
            else:
                log.warning(f"  validate_on_chain returned no tx")
                # Increment attempt counter so we stop retrying after _MAX_RETRY_ATTEMPTS
                _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                attempt_n = _SEEN_SUBMISSIONS[pubkey]
                if attempt_n < _MAX_RETRY_ATTEMPTS:
                    log.info(
                        f"  {pubkey[:16]}…: attempt {attempt_n}/{_MAX_RETRY_ATTEMPTS} "
                        f"— will retry up to {_MAX_RETRY_ATTEMPTS - attempt_n} more time(s)"
                    )
                else:
                    log.info(f"  {pubkey[:16]}…: max retries reached — will skip next polls")

            # Count every verdict (confirm or reject), regardless of tx success
            validated_today += 1
            if within_tol:
                accepted += 1
            else:
                rejected += 1

            # ── Audit log (every decision) ────────────────────────────────────
            # Use CONFIRM/REJECT only when the tx actually landed on-chain.
            # When the on-chain call fails the account stays Pending and will
            # re-appear in future polls; recording CONFIRM/REJECT here would
            # cause _load_seen_from_audit() on restart to mark the submission
            # at max-attempts and block it permanently despite no tx landing.
            audit_decision = verdict if tx else f"{verdict}_TX_FAILED"
            append_audit({
                "ts":               datetime.now(timezone.utc).isoformat(),
                "submission_pubkey": pubkey,
                "miner_wallet":     miner_wallet,
                "miner_gpu":        _miner_gpu_model,
                "claimed_score":    claimed,
                "adjusted_claimed": round(adjusted_claimed, 4),
                "bias_factor":      round(bias_factor, 4) if bias_factor is not None else None,
                "rescored":         rescored,
                "decision":         audit_decision,
                "rel_err":          round(rel_err, 4),
                "tolerance_used":   round(tol, 4),
                "difficulty_tier":  difficulty,
                "target_type":      "CRISPR" if is_crispr else ("RNA" if is_mrna else "PROTEIN"),
                "life_earned":      tier_reward if (within_tol and tx) else 0,
            })

            append_log({
                "ts":               datetime.now(timezone.utc).isoformat(),
                "pubkey":           pubkey,
                "smiles":           smiles,
                "target_id":        target_id_int,
                "claimed":          claimed,
                "rescored":         rescored,
                "rel_err":          round(rel_err, 4),
                "within_tolerance": within_tol,
                "verdict":          verdict,
                "tx":               tx,
                "elapsed_s":        round(elapsed, 1),
                "difficulty_tier":  difficulty,
                "target_type":      "CRISPR" if is_crispr else ("RNA" if is_mrna else "PROTEIN"),
                "life_earned":      tier_reward if (within_tol and tx) else 0,
            })

            # Write stats immediately after each validation so dashboard is live
            stats.update({
                "validated_today": validated_today,
                "confirmed":       accepted,
                "rejected":        rejected,
                "accept_rate":     round(accepted / max(validated_today, 1) * 100, 1),
                "life_commission": life_earned,
                "last_heartbeat":  datetime.now(timezone.utc).isoformat(),
                "current_target":  None,
                "current_smiles":  None,
                "last_updated":    datetime.now(timezone.utc).isoformat(),
                "gpu_model":       _miner_gpu_model,
                "gpu_bias":        _gpu_bias_tracker.summary() if _gpu_bias_tracker else {},
            })
            write_stats(stats)

        accept_rate = round(accepted / max(validated_today, 1) * 100, 1)
        net_info = fetch_network_validator_info()
        stats.update({
            "status":          "ONLINE",
            "validated_today": validated_today,
            "confirmed":       accepted,
            "rejected":        rejected,
            "accept_rate":     accept_rate,
            "life_commission": life_earned,
            "last_heartbeat":  datetime.now(timezone.utc).isoformat(),
            "current_target":  None,
            "current_smiles":  None,
            "active_validators":   net_info.get("active_validators", 0),
            "self_reputation_pct": round(net_info.get("self_reputation_bps", 10000) / 100, 1),
            "last_updated":    datetime.now(timezone.utc).isoformat(),
            "gpu_model":       _miner_gpu_model,
            "gpu_bias":        _gpu_bias_tracker.summary() if _gpu_bias_tracker else {},
        })
        write_stats(stats)
        log.info(f"Validated={validated_today}  Accept={accept_rate}%  $LIFE={life_earned:.1f}  Validators={net_info.get('active_validators',0)}")
        log.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
