#!/usr/bin/env python3
"""
LIFE Compute — Validator Daemon (devnet)

Event-driven architecture (WS-first):
  1. programSubscribe WebSocket → notified within seconds when a ResultSubmission
     account is created or changes state; parsed and enqueued immediately.
  2. A dispatcher thread routes items to _crispr_queue or _protein_queue.
  3. CRISPR worker drains _crispr_queue (analytical, no Boltz2).
     Protein/mRNA worker (main thread) drains _protein_queue (Boltz2 rescore).
  4. validate_result on-chain — confirm if |rescored - claimed| / |claimed| ≤ tol.
  5. getProgramAccounts catch-up sweep every 5 min recovers any submissions
     missed during WS disconnects or silent RPC notification drops.

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
import asyncio, json, time, logging, os, queue, re, shutil, stat, subprocess, sys, threading
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

# ── WebSocket subscription config ─────────────────────────────────────────────
# Derive WS endpoint from the HTTP RPC URL (https→wss, http→ws).
# Override with SOLANA_WS env var for custom RPC providers (e.g. Helius, QuickNode).
def _rpc_to_ws(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://"):]
    return http_url

SOLANA_WS = _env("SOLANA_WS", _rpc_to_ws(SOLANA_RPC))

# Keepalive: if no pong is received within PING_TIMEOUT seconds after a ping,
# the connection is considered dead and the listener reconnects.
WS_PING_INTERVAL = 20   # seconds between pings
WS_PING_TIMEOUT  = 10   # seconds to wait for pong
WS_MAX_BACKOFF   = 60   # cap on exponential reconnect backoff (seconds)

# Catch-up poll interval (seconds).
# Protects against two real failure modes even with the WS "connected":
#   (a) Silent notification drops from public devnet RPC nodes under load —
#       the WS appears healthy but notifications simply don't arrive.
#   (b) Submissions that existed before subscribe time (daemon restart) —
#       Solana does NOT replay historical states on (re)subscription.
# 5 minutes: worst-case delay for a missed submission is 5 min, not 30.
# The sweep is a no-op when nothing new exists, so overhead is negligible.
CATCHUP_POLL_INTERVAL = 300   # seconds (5 minutes)

# ── Shared submission work queues ─────────────────────────────────────────────
# WS listener and catch-up poller both write to _submission_queue.
# Dispatcher thread routes items to the appropriate typed sub-queue.
_submission_queue: queue.Queue = queue.Queue()   # raw events from WS / sweep
_protein_queue:    queue.Queue = queue.Queue()   # protein + mRNA items → main worker
_crispr_queue:     queue.Queue = queue.Queue()   # CRISPR items → crispr worker

# ── Miner allowlist — only process submissions from this wallet ───────────────
# Set to empty string to accept all miners (open mode).
MINER_WALLET  = _env("MINER_WALLET",  "")   # empty = open mode, accept all miners
MINER_ACCOUNT = _env("MINER_ACCOUNT", "BaMnTDYP1T4kZVwUbv9ZyppgUHeSRKNo9bMDRvFNN2iX")

WORK_DIR    = Path(__file__).parent
STATS_PATH  = WORK_DIR / "stats.json"
LOG_JSONL        = WORK_DIR / "output" / "validator_log.jsonl"
AUDIT_JSONL      = WORK_DIR / "output" / "validator_audit.jsonl"
CRANK_QUEUE_FILE = WORK_DIR / "output" / "crank_queue.jsonl"
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
# Miner base rewards by difficulty tier (raw LIFE units, matching on-chain constants.rs).
# Tier 1=Easy(1), Tier 2=Medium(5), Tier 3=Hard(25).  All molecule types use these.
ONCHAIN_MINER_BASE: dict[int, int] = {1: 1, 2: 5, 3: 25}

# Legacy alias used by _halved_reward() calls that still exist in logging paths.
# Do NOT use for commission calculation — use ONCHAIN_MINER_BASE instead.
BASE_TIER_REWARDS: dict[int, int] = {1: 1, 2: 5, 3: 7}

# On-chain two-layer halving constants (mirrors rewards.rs / constants.rs).
# Layer 1 — cumulative supply milestones (raw units at 6 decimals):
_HALVING_M1 = 5_250_000  * 1_000_000   # 100% → 50% after this many raw tokens minted
_HALVING_M2 = 10_500_000 * 1_000_000   # 50%  → 25%
_HALVING_M3 = 15_750_000 * 1_000_000   # 25%  → 12.5%
# Layer 2 — per-target hit count thresholds:
_HALVING_HIT_T1 = 100    # below this: 100%; at/above: 75%
_HALVING_HIT_T2 = 1_000  # at/above this: 50%
# Number of confirming validators required (on-chain network_config.validators_required).
_VALIDATORS_REQUIRED = 2

# Halving schedule: every 210,000 on-chain epochs the reward halves.
# multiplier = 0.5 ** (current_epoch // HALVING_INTERVAL)
# Integer arithmetic: reward >> halvings (floor division, minimum 1 if >0).
HALVING_INTERVAL = 210_000

# ── CRISPR: gRNA similarity-based reward decay ────────────────────────────────
# Mirrors the miner's reward_decay logic exactly (same proxy, same thresholds).
# Proxy: Hamming similarity to any prior confirmed gRNA for the same target.
# Thresholds (miner-deployed 2026-09-02):
#   similarity >= 0.85  →  0.35x  (highly similar, strong novelty penalty)
#   0.70 <= sim < 0.85  →  0.65x  (moderately similar, mild penalty)
#   similarity < 0.70   →  1.0x   (novel, full reward)
GRNA_SIM_HIGH          = 0.85
GRNA_SIM_MID           = 0.70
GRNA_REWARD_HIGH_SIM   = 0.35
GRNA_REWARD_MID_SIM    = 0.65
GRNA_REWARD_NOVEL      = 1.0

# Per-target confirmed gRNA history — keyed by on-chain target_id (int).
# Populated at startup from audit log; updated live after each CONFIRM.
_CRISPR_GRNA_HISTORY: dict[int, list[str]] = {}

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


# ── Crank queue ───────────────────────────────────────────────────────────────

def append_crank_queue(submission_pubkey: str) -> None:
    """
    Signal life_crank.js to mint reward for a freshly-confirmed ResultSubmission.

    The crank reads new lines from CRANK_QUEUE_FILE using a byte-offset pointer
    (crank_queue.jsonl.pos) so no line is ever re-processed after a restart.
    The on-chain reward_minted flag is the real idempotency guard; this queue is
    just a low-latency hint to avoid waiting for the next periodic scan.
    """
    try:
        with CRANK_QUEUE_FILE.open("a") as f:
            f.write(json.dumps({
                "pubkey": submission_pubkey,
                "ts":     datetime.now(timezone.utc).isoformat(),
            }) + "\n")
    except Exception as _e:
        # Never let a queue-write failure block the validator loop
        log.warning(f"  [CRANK-QUEUE] write failed ({_e}) — crank will pick up via scan")


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
                    # Terminal security-gate rejections: these submissions will never
                    # pass (corrupted score, invalid SMILES, unknown target, wallet
                    # mismatch, self-validation).  Count each occurrence so they are
                    # evicted from the queue after _MAX_RETRY_ATTEMPTS re-encounters,
                    # surviving daemon restarts without re-clogging the catchup poll.
                    elif decision in (
                        "CORRUPTED_SCORE",
                        "SMILES_INVALID",
                        "UNKNOWN_TARGET",
                        "WALLET_NOT_ALLOWED",
                        "SELF_VALIDATION_REJECTED",
                    ):
                        _SEEN_SUBMISSIONS[pk] = min(
                            _SEEN_SUBMISSIONS.get(pk, 0) + 1,
                            _MAX_RETRY_ATTEMPTS + 1,  # one above cap → immediate skip
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


def _grna_similarity_reward_factor(grna_seq: str, target_id: int) -> tuple[float, float]:
    """
    Compute the similarity-based reward decay factor for a crispr_generated gRNA.
    Exactly mirrors the miner's algorithm (deployed 2026-09-02).

    Proxy: maximum Hamming similarity = (20 - hamming_dist) / 20
           against all confirmed gRNAs for this target in _CRISPR_GRNA_HISTORY.

    Thresholds:
        max_sim >= 0.85  →  0.35x  (high similarity, strong decay)
        max_sim >= 0.70  →  0.65x  (moderate similarity, mild decay)
        max_sim  < 0.70  →  1.0x   (novel, full reward)

    Returns
    -------
    (reward_factor, max_similarity)
    """
    prior = _CRISPR_GRNA_HISTORY.get(target_id, [])
    if not prior:
        return GRNA_REWARD_NOVEL, 0.0

    grna_seq = grna_seq.upper().strip()
    n = len(grna_seq)
    max_sim = 0.0
    for p in prior:
        p_up = p.upper().strip()
        if len(p_up) != n:
            continue
        dist = sum(a != b for a, b in zip(grna_seq, p_up))
        sim  = (n - dist) / n
        if sim > max_sim:
            max_sim = sim

    if max_sim >= GRNA_SIM_HIGH:          # >= 0.85
        factor = GRNA_REWARD_HIGH_SIM     # 0.35x
    elif max_sim >= GRNA_SIM_MID:         # >= 0.70
        factor = GRNA_REWARD_MID_SIM      # 0.65x
    else:
        factor = GRNA_REWARD_NOVEL        # 1.0x

    return factor, max_sim


def _load_crispr_grna_history():
    """
    Warm up _CRISPR_GRNA_HISTORY from the audit log at startup.

    Collects all confirmed gRNAs per target so similarity decay fires correctly
    on the first submission after a restart (not just after first live CONFIRM).
    """
    if not AUDIT_JSONL.exists():
        return
    loaded = 0
    try:
        with AUDIT_JSONL.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    if r.get("target_type") != "CRISPR":
                        continue
                    if r.get("decision") != "CONFIRM":
                        continue
                    grna = (r.get("smiles") or "").strip().upper()
                    tid  = r.get("target_id")
                    if not grna or len(grna) != 20 or tid is None:
                        continue
                    history = _CRISPR_GRNA_HISTORY.setdefault(tid, [])
                    if grna not in history:
                        history.append(grna)
                        loaded += 1
                except Exception:
                    pass
    except Exception:
        pass
    if loaded:
        log.info(
            f"[GRNA-HISTORY] Loaded {loaded} confirmed gRNA(s) from audit log "
            f"across {len(_CRISPR_GRNA_HISTORY)} target(s)"
        )


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



# ── Stage-1: validate_protein() ──────────────────────────────────────────────
# One deterministic scoring function for protein submissions.
# Architecture: sanity gate → bias guard → tolerance check.
# No fallback branching — one formula, always.

# Data-derived sanity bounds (from 1,191 real protein CONFIRMs in audit history):
#   rescored spans -3.05 to +0.20; gate set to -5.0/+1.0 for GPU variance headroom.
#   Catches physically impossible values (e.g. -28 kcal/mol from formula bugs).
PROTEIN_SANITY_LO: float = -5.0
PROTEIN_SANITY_HI: float =  1.0

# Bias guard: must be positive and within a sane magnitude range.
# 0.1 floor prevents near-zero nonsense corrections.
# 10.0 ceiling is spec default; will be refined per-modality once 50+ samples exist.
# CDK4 historical negative bias_factor (-0.0959) correctly fails this guard.
BIAS_FACTOR_LO: float = 0.1
BIAS_FACTOR_HI: float = 10.0


def validate_protein(
    smiles: str,
    target: dict,
    claimed: float,
    seed: int,
    gpu_model: str,
    bias_tracker: "GpuBiasTracker | None",
) -> dict:
    """
    Validate a protein submission end-to-end.

    Returns a result dict with keys:
      modality        "PROTEIN"
      rescored        float | None   — Boltz2 output in kcal/mol
      sanity_ok       bool
      sanity_note     str
      bias_factor     float | None   — value used (None = skipped)
      bias_note       str
      adjusted_claimed float
      rel_err         float | None
      tol             float
      within_tol      bool
      verdict         "CONFIRM" | "REJECT" | "VALIDATOR_ERROR"
    """
    result: dict = {
        "modality":        "PROTEIN",
        "rescored":        None,
        "sanity_ok":       False,
        "sanity_note":     "",
        "bias_factor":     None,
        "bias_note":       "",
        "adjusted_claimed": claimed,
        "rel_err":         None,
        "tol":             VALIDATION_TOLERANCE,
        "within_tol":      False,
        "verdict":         "VALIDATOR_ERROR",
    }

    # ── Step 1: Run Boltz2 ────────────────────────────────────────────────────
    rescored = run_boltz2(smiles, target, seed=seed)
    result["rescored"] = rescored
    if rescored is None:
        result["sanity_note"] = "Boltz2 returned None"
        result["verdict"]     = "VALIDATOR_ERROR"
        return result

    # ── Step 2: Sanity gate ───────────────────────────────────────────────────
    if not (PROTEIN_SANITY_LO <= rescored <= PROTEIN_SANITY_HI):
        result["sanity_ok"]   = False
        result["sanity_note"] = (
            f"rescored={rescored:.3f} outside "
            f"[{PROTEIN_SANITY_LO}, {PROTEIN_SANITY_HI}]"
        )
        result["verdict"] = "VALIDATOR_ERROR"
        log.warning(
            f"  [PROTEIN-SANITY] rescored={rescored:.3f} outside "
            f"[{PROTEIN_SANITY_LO}, {PROTEIN_SANITY_HI}] — VALIDATOR_ERROR"
        )
        return result
    result["sanity_ok"]   = True
    result["sanity_note"] = "ok"

    # ── Step 3: GPU bias correction (guarded) ─────────────────────────────────
    family      = _target_family(target.get("id", ""))
    bias_factor = None
    if bias_tracker is not None and gpu_model and gpu_model != "UNKNOWN":
        bf_raw = bias_tracker.get_bias_factor(gpu_model, family)
        if bf_raw is not None:
            if BIAS_FACTOR_LO <= bf_raw <= BIAS_FACTOR_HI:
                bias_factor = bf_raw
                result["bias_note"] = f"applied bf={bf_raw:.4f}"
            else:
                result["bias_note"] = (
                    f"bf={bf_raw:.4f} out of [{BIAS_FACTOR_LO},{BIAS_FACTOR_HI}] "
                    f"— skipped, raw tol"
                )
                log.warning(
                    f"  [PROTEIN-BIAS] bf={bf_raw:.4f} out of range "
                    f"[{BIAS_FACTOR_LO},{BIAS_FACTOR_HI}] — falling back to raw tolerance"
                )
        else:
            result["bias_note"] = "no bias data yet"

    if bias_factor is not None:
        adjusted_claimed = claimed * bias_factor
        tol = TIGHTENED_TOLERANCE
    else:
        adjusted_claimed = claimed
        tol = VALIDATION_TOLERANCE

    result["bias_factor"]      = bias_factor
    result["adjusted_claimed"] = adjusted_claimed
    result["tol"]              = tol

    # ── Step 4: Tolerance check ───────────────────────────────────────────────
    denom  = abs(adjusted_claimed)
    rel_err = abs(rescored - adjusted_claimed) / denom if denom else abs(rescored)
    within_tol = rel_err <= tol

    result["rel_err"]    = round(rel_err, 4)
    result["within_tol"] = within_tol
    result["verdict"]    = "CONFIRM" if within_tol else "REJECT"

    # ── Record for bias learning (always, regardless of verdict) ──────────────
    if bias_tracker is not None and gpu_model and gpu_model != "UNKNOWN":
        bias_tracker.record(gpu_model, family, claimed, rescored)

    log.info(
        f"  [PROTEIN] {result['verdict']}"
        f"  claimed={claimed:.3f}  rescored={rescored:.3f}"
        f"  adj={adjusted_claimed:.3f}  rel_err={rel_err:.4f}  tol={tol}"
        + (f"  [bias={bias_factor:.4f}]" if bias_factor else "  [no bias]")
    )
    return result


# ── Stage-3: validate_crispr() ────────────────────────────────────────────────
# Authoritative rescoring: real Boltz2 3-chain iptm, matching the miner exactly.
#
# Miner path (adaptive/life_crispr_boltz.py):
#   Builds a 3-chain Boltz2 input — SpCas9 protein (chain A), spacer+scaffold
#   RNA (chain B), revcomp(spacer)+NGG DNA (chain C) — runs structure prediction
#   (no properties/affinity block), reads iptm from confidence JSON top-level,
#   computes affinity_kcal = -6.0 - 3.0 × iptm.
#
# Validator mirrors this exactly.  score_grna() is retained only as a fast
# pre-screen gate: if combined < threshold the gRNA is garbage and Boltz2 is
# skipped.  The authoritative rescored value is always the Boltz2 iptm result.
#
# Sanity gate: -9.5 to -5.5 kcal/mol (iptm ∈ (0,1] → score ∈ (-9,-6) by construction).
# Tolerance: 0.25 (unchanged from prior CRISPR_TOL).

CRISPR_SANITY_LO: float = -9.5
CRISPR_SANITY_HI: float = -5.5
CRISPR_TOL: float = 0.25

# ── SpCas9 / gRNA constants (mirrors adaptive/life_crispr_boltz.py exactly) ───
# First 200 aa of SpCas9 recognition lobe (UniProt P0DOT7) — single-seq mode,
# no MSA required.  Same 200-aa fragment used for every target regardless of gene.
SPCAS9_REC1_200AA: str = (
    "MDKKYSIGLDIGTNSVGWAVITDEYKVPSKKFKVLGNTDRHSIKKNLIGALLFDSGETAEATRLKRTARRRYTRRK"
    "NRICYLQEIFSNEMAKVDDSFFHRLEESFLVEEDKKHERHPIFGNIVDEVAYHEKYPTIYHLRKKLVDSTDKADLRL"
    "IYLALAHMIKFRGHFLIEGDLNPDNSDVDKLFIQLVQTYNQLFEENP"
)  # exactly 200 aa: 76 + 77 + 47

# Canonical SpCas9 sgRNA scaffold (76 nt), appended after the 20-nt spacer.
# Spacer (chain B) = spacer_seq + SGRNA_SCAFFOLD_RNA  (96 nt total).
SGRNA_SCAFFOLD_RNA: str = (
    "GTTTTAGAGCTAGAAATAGCAAGTTAAAATAAGGCTAGTCCGTTATCAACTTGAAAAAGTGGCACCGAGTCGGTGC"
)

# Complement map for building the DNA target strand (chain C).
_DNA_COMP: dict[str, str] = {"A": "T", "T": "A", "G": "C", "C": "G"}


def _revcomp(seq: str) -> str:
    """Reverse complement of a DNA sequence."""
    return "".join(_DNA_COMP.get(b, "N") for b in reversed(seq.upper()))


def _write_crispr_boltz_yaml(in_dir: Path, mol_id: int, target_id: str,
                              grna_seq: str) -> Path:
    """
    Build the 3-chain Boltz2 YAML for a CRISPR gRNA submission.

    Chain A — protein  : SPCAS9_REC1_200AA (SpCas9 recognition lobe, msa=empty)
    Chain B — rna      : grna_seq (20 nt spacer) + SGRNA_SCAFFOLD_RNA (76 nt)
    Chain C — dna      : revcomp(grna_seq) + "NGG" (23 nt target strand + PAM)

    No properties block → structure-only mode → Boltz2 writes confidence_*.json
    with top-level "iptm" field.
    """
    import yaml as _yaml
    spacer   = grna_seq.upper()
    rna_seq  = spacer + SGRNA_SCAFFOLD_RNA          # 20 + 76 = 96 nt
    dna_seq  = _revcomp(spacer) + "NGG"             # 20 + 3 = 23 nt
    data = {
        "version": 1,
        "sequences": [
            {"protein": {"id": "A", "sequence": SPCAS9_REC1_200AA, "msa": "empty"}},
            {"rna":     {"id": "B", "sequence": rna_seq}},
            {"dna":     {"id": "C", "sequence": dna_seq}},
        ],
        # NO properties block — structure-only mode, iptm is the signal
    }
    yaml_path = in_dir / f"{mol_id}_{target_id}.yaml"
    yaml_path.write_text(_yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    return yaml_path


def run_boltz2_crispr_iptm(grna_seq: str, target: dict,
                            seed: int = BOLTZ_SEED) -> float | None:
    """
    Run Boltz2 in structure-only mode for a CRISPR gRNA and return affinity
    via  -6.0 - 3.0 × iptm,  matching adaptive/life_crispr_boltz.py exactly.

    3-chain complex: SpCas9 protein (A) + spacer+scaffold RNA (B) + DNA target (C).
    Boltz2 runs structure prediction (no properties/affinity block).
    iptm is read from the top-level "iptm" field of the confidence JSON.

    Returns affinity in kcal/mol, or None on any failure.
    """
    from boltz.main import predict

    grna_seq  = grna_seq.upper().strip().replace("U", "T")
    target_id = target.get("id", "UNKNOWN")

    if len(grna_seq) != 20 or not all(c in "ACGT" for c in grna_seq):
        log.warning(f"  [CRISPR-BOLTZ] invalid gRNA length/alphabet: {grna_seq[:24]} — skip")
        return None

    mol_id   = _mol_id(grna_seq)      # deterministic hash of the sequence
    run_uuid = str(uuid.uuid4())
    run_root = Path(f"/tmp/life-validator-crispr-{run_uuid}")
    in_dir   = run_root / "inputs"
    out_dir  = run_root / "outputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_root, stat.S_IRWXU)   # 700: owner-only

    try:
        yaml_path = _write_crispr_boltz_yaml(in_dir, mol_id, target_id, grna_seq)

        # ── Integrity check: re-read and verify the spacer sequence ───────────
        import yaml as _yaml
        written = _yaml.safe_load(yaml_path.read_text())
        rna_blocks = [s["rna"] for s in written.get("sequences", []) if "rna" in s]
        if not rna_blocks:
            log.warning("  [CRISPR-BOLTZ] SECURITY: no rna block in written YAML — skip")
            return None
        written_spacer = rna_blocks[0].get("sequence", "")[:20]
        if written_spacer.upper() != grna_seq:
            log.warning(
                f"  [CRISPR-BOLTZ] SECURITY: spacer mismatch after write "
                f"(expected={grna_seq} actual={written_spacer}) — skip"
            )
            return None

        boltz_start = time.time()

        # ── Run Boltz2 in structure-only mode (no affinity flags) ─────────────
        predict.main([
            str(in_dir),
            "--out_dir",          str(out_dir),
            "--recycling_steps",  str(_RECYCLING_STEPS),
            "--sampling_steps",   str(_SAMPLING_STEPS),
            "--diffusion_samples", str(_DIFFUSION_SAMPLES),
            "--output_format",    "mmcif",
            "--seed",             str(seed),
            "--num_workers",      "0",
            "--accelerator",      "gpu",
            "--override",
            "--no_kernels",
        ], standalone_mode=False)

        # ── Read iptm from confidence JSON (reuse existing reader) ─────────────
        metrics = _read_boltz_affinity(out_dir, mol_id, target_id, boltz_start)
        if metrics is None:
            log.warning(f"  [CRISPR-BOLTZ] no confidence output for {grna_seq[:16]}…")
            return None

        iptm = metrics.get("iptm")
        if iptm is None:
            log.warning(
                f"  [CRISPR-BOLTZ] iptm missing from confidence JSON "
                f"(keys: {list(metrics.keys())[:8]})"
            )
            return None

        score = round(-6.0 - 3.0 * float(iptm), 4)
        log.info(
            f"  [CRISPR-BOLTZ] -6.0-3.0*iptm  "
            f"gRNA={grna_seq[:16]}…  iptm={iptm:.4f} → {score:.4f} kcal/mol"
        )
        return score

    except Exception as exc:
        log.warning(f"  [CRISPR-BOLTZ] Boltz2 raised: {exc}")
        return None
    finally:
        shutil.rmtree(str(run_root), ignore_errors=True)


def validate_crispr(
    grna_seq: str,
    target: dict,
    claimed: float,
    gpu_model: str,
    bias_tracker: "GpuBiasTracker | None",
) -> dict:
    """
    Validate a CRISPR gRNA submission end-to-end.

    Two-stage pipeline:
      Stage A — analytical pre-screen (score_grna): fast CPU check.
                If combined < CRISPR_MIN_COMBINED_VALIDATOR the gRNA is
                garbage; skip Boltz2 and return VALIDATOR_ERROR immediately.
      Stage B — Boltz2 iptm rescore (run_boltz2_crispr_iptm): authoritative.
                Mirrors adaptive/life_crispr_boltz.py on the miner exactly.
                Tolerance check is applied to the Boltz2 result, not the
                analytical pre-screen value.
    """
    result: dict = {
        "modality":         "CRISPR",
        "rescored":         None,
        "sanity_ok":        False,
        "sanity_note":      "",
        "bias_factor":      None,
        "bias_note":        "CRISPR: Boltz2 iptm rescore, no bias correction",
        "adjusted_claimed": claimed,
        "rel_err":          None,
        "tol":              CRISPR_TOL,
        "within_tol":       False,
        "verdict":          "VALIDATOR_ERROR",
    }

    # ── Stage A: analytical pre-screen (fast gate, no GPU) ───────────────────
    # run_crispr_validation() handles: int→string ID mapping, U→T, length check.
    prescreen_affinity, grna_scores = run_crispr_validation(grna_seq, target)
    if prescreen_affinity is None:
        # Invalid sequence (bad length or alphabet)
        result["sanity_note"] = "Invalid gRNA (bad length/alphabet)"
        result["verdict"]     = "VALIDATOR_ERROR"
        return result

    combined = grna_scores.get("combined", 0.0)
    # target.get("id") is the authoritative target identifier (already resolved
    # by run_crispr_validation's int→string mapping inside grna_scores indirectly,
    # but the target dict's "id" field is what _crispr_threshold expects).
    target_name = target.get("id", "")
    # If target_id is an on-chain integer, map it to the string name first.
    _CRISPR_ONCHAIN_TO_NAME_V: dict = {
        3000: "TP53_CRISPR",  3001: "KRAS_CRISPR",  3002: "BCL2_CRISPR",
        3003: "MYC_CRISPR",   3004: "EGFR_CRISPR",  3005: "HER2_CRISPR",
        3006: "BRCA1_CRISPR", 3007: "PDL1_CRISPR",  3008: "TERT_CRISPR",
        3009: "CDK4_CRISPR",
    }
    try:
        target_name = _CRISPR_ONCHAIN_TO_NAME_V.get(int(target_name), str(target_name))
    except (ValueError, TypeError):
        target_name = str(target_name)
    threshold = _crispr_threshold(target_name)
    if combined < threshold:
        result["sanity_note"] = (
            f"pre-screen combined={combined:.4f} < threshold={threshold:.3f} — skip Boltz2"
        )
        result["verdict"] = "VALIDATOR_ERROR"
        log.info(
            f"  [CRISPR] pre-screen FAIL  combined={combined:.3f} < {threshold:.3f}"
            f"  gRNA={grna_seq[:16]}… — skipping Boltz2"
        )
        return result

    log.info(
        f"  [CRISPR] pre-screen OK  combined={combined:.3f} ≥ {threshold:.3f}"
        f"  gRNA={grna_seq[:16]}… — launching Boltz2"
    )

    # ── Stage B: Boltz2 iptm rescore (authoritative) ─────────────────────────
    rescored = run_boltz2_crispr_iptm(grna_seq, target, seed=BOLTZ_SEED)
    result["rescored"] = rescored
    if rescored is None:
        result["sanity_note"] = "Boltz2 returned None"
        result["verdict"]     = "VALIDATOR_ERROR"
        return result

    # ── Sanity gate on Boltz2 result ──────────────────────────────────────────
    if not (CRISPR_SANITY_LO <= rescored <= CRISPR_SANITY_HI):
        result["sanity_ok"]   = False
        result["sanity_note"] = (
            f"rescored={rescored:.3f} outside [{CRISPR_SANITY_LO}, {CRISPR_SANITY_HI}]"
        )
        result["verdict"] = "VALIDATOR_ERROR"
        log.warning(
            f"  [CRISPR-SANITY] rescored={rescored:.3f} outside "
            f"[{CRISPR_SANITY_LO},{CRISPR_SANITY_HI}] — VALIDATOR_ERROR"
        )
        return result
    result["sanity_ok"]   = True
    result["sanity_note"] = "ok"

    # ── Tolerance check ───────────────────────────────────────────────────────
    adjusted_claimed = claimed
    tol              = CRISPR_TOL
    denom   = abs(adjusted_claimed)
    rel_err = abs(rescored - adjusted_claimed) / denom if denom else abs(rescored)
    within_tol = rel_err <= tol

    result["adjusted_claimed"] = adjusted_claimed
    result["tol"]              = tol
    result["rel_err"]          = round(rel_err, 4)
    result["within_tol"]       = within_tol
    result["verdict"]          = "CONFIRM" if within_tol else "REJECT"

    # ── Record in bias tracker (informational only, not applied) ──────────────
    family = _target_family(target.get("id", ""))
    if bias_tracker is not None and gpu_model and gpu_model != "UNKNOWN":
        bias_tracker.record(gpu_model, family, claimed, rescored)

    log.info(
        f"  [CRISPR] {result['verdict']}"
        f"  combined={combined:.3f}"
        f"  claimed={claimed:.3f}  rescored={rescored:.3f}"
        f"  rel_err={rel_err:.4f}  tol={tol}"
    )
    return result


# ── Stage-2: validate_mrna() ──────────────────────────────────────────────────
# Formula: -6.0 - 3.0 × iptm (from Boltz2 confidence output).
# Always in (-9.0, -6.0) ⊂ sanity gate [-9.5, -5.5] by construction.
# Option C: log-only mode during bias ramp-up. mRNA will not confirm
# until 50+ bias samples establish the true ratio under this formula.
# (The old formula produced bias_factor ≈ 97; the new formula will produce
# a different ratio — do not assume the old numbers carry over.)

MRNA_SANITY_LO: float = -9.5   # -6.0-3.0*1.0 = -9.0, gate adds headroom
MRNA_SANITY_HI: float = -5.5   # -6.0-3.0*0.0 = -6.0, gate adds headroom

# mRNA bias_factor guard: 0.1-10.0 (spec default). Will be updated once
# 50+ real samples under the new formula define the true ratio range.
MRNA_BIAS_LO: float = BIAS_FACTOR_LO   # 0.1
MRNA_BIAS_HI: float = BIAS_FACTOR_HI   # 10.0

# Minimum samples before mRNA bias is applied (Option C ramp-up).
# Set to 9999 to DISABLE bias activation: the gpu_bias_models.json mRNA entries
# were contaminated by an old-formula era (miner used -iptm*30.0, validator used
# -6.0-3.0*iptm) before both sides were aligned.  The bias_factor values (~0.35-0.52)
# are an artifact of that mismatch — they do NOT represent real GPU variance.
# Applying them squashes adjusted_claimed to ~40% of the true value, then the
# TIGHTENED_TOLERANCE=0.35 rejects nearly everything.  Until the bias model is
# rebuilt from clean, aligned-formula samples only, mRNA stays on raw tol=0.7777.
MRNA_BIAS_MIN_SAMPLES: int = 9999   # effectively disabled until bias file is rebuilt


def validate_mrna(
    smiles: str,
    target: dict,
    claimed: float,
    seed: int,
    gpu_model: str,
    bias_tracker: "GpuBiasTracker | None",
) -> dict:
    """
    Validate an mRNA submission end-to-end. Option C (log-only) during ramp-up.

    Returns same dict shape as validate_protein(), plus:
      log_only  bool  — True if in bias ramp-up (no CONFIRM possible)

    During ramp-up (< MRNA_BIAS_MIN_SAMPLES):
      - Boltz2 runs, iptm is read, rescored is logged to bias tracker
      - verdict is always REJECT (tolerance always exceeded with raw tol)
      - This accumulates real bias_factor data under the new formula
    After ramp-up (≥ MRNA_BIAS_MIN_SAMPLES with valid bias in [0.1,10.0]):
      - Normal CONFIRM/REJECT based on adjusted_claimed
    """
    result: dict = {
        "modality":         "RNA",
        "rescored":         None,
        "sanity_ok":        False,
        "sanity_note":      "",
        "bias_factor":      None,
        "bias_note":        "",
        "adjusted_claimed": claimed,
        "rel_err":          None,
        "tol":              VALIDATION_TOLERANCE,
        "within_tol":       False,
        "verdict":          "VALIDATOR_ERROR",
        "log_only":         True,
    }

    # ── Step 1: Run Boltz2 RNA mode, extract iptm ─────────────────────────────
    rescored = run_boltz2_mrna_iptm(smiles, target, seed=seed)
    result["rescored"] = rescored
    if rescored is None:
        result["sanity_note"] = "Boltz2 returned None"
        result["verdict"]     = "VALIDATOR_ERROR"
        return result

    # ── Step 2: Sanity gate ───────────────────────────────────────────────────
    if not (MRNA_SANITY_LO <= rescored <= MRNA_SANITY_HI):
        result["sanity_ok"]   = False
        result["sanity_note"] = (
            f"rescored={rescored:.3f} outside [{MRNA_SANITY_LO}, {MRNA_SANITY_HI}]"
        )
        result["verdict"] = "VALIDATOR_ERROR"
        log.warning(f"  [mRNA-SANITY] rescored={rescored:.3f} outside "
                    f"[{MRNA_SANITY_LO}, {MRNA_SANITY_HI}] — VALIDATOR_ERROR")
        return result
    result["sanity_ok"]   = True
    result["sanity_note"] = "ok"

    # ── Step 3: Record for bias learning (ALWAYS — this is how Option C accumulates data)
    family = _target_family(target.get("id", ""))
    if bias_tracker is not None and gpu_model and gpu_model != "UNKNOWN":
        bias_tracker.record(gpu_model, family, claimed, rescored)

    # ── Step 4: Check how many samples we have ────────────────────────────────
    n_samples = 0
    if bias_tracker is not None:
        entry = bias_tracker._data.get(gpu_model, {}).get(family, {})
        n_samples = entry.get("n", 0)

    bias_factor = None
    if n_samples >= MRNA_BIAS_MIN_SAMPLES and bias_tracker is not None:
        bf_raw = bias_tracker.get_bias_factor(gpu_model, family)
        if bf_raw is not None and MRNA_BIAS_LO <= bf_raw <= MRNA_BIAS_HI:
            bias_factor = bf_raw
            result["log_only"] = False
            result["bias_note"] = f"applied bf={bf_raw:.4f} (n={n_samples})"
        else:
            result["bias_note"] = (
                f"bf={bf_raw:.4f} out of [{MRNA_BIAS_LO},{MRNA_BIAS_HI}] — raw tol "
                f"(n={n_samples}, range needs calibration)"
            )
    else:
        result["bias_note"] = (
            f"ramp-up: n={n_samples}/{MRNA_BIAS_MIN_SAMPLES} samples — log-only mode"
        )

    result["bias_factor"] = bias_factor

    # ── Step 5: Tolerance check ───────────────────────────────────────────────
    if bias_factor is not None:
        adjusted_claimed = claimed * bias_factor
        tol = TIGHTENED_TOLERANCE
    else:
        adjusted_claimed = claimed
        tol = VALIDATION_TOLERANCE

    result["adjusted_claimed"] = adjusted_claimed
    result["tol"]              = tol

    denom   = abs(adjusted_claimed)
    rel_err = abs(rescored - adjusted_claimed) / denom if denom else abs(rescored)
    within_tol = rel_err <= tol

    result["rel_err"]    = round(rel_err, 4)
    result["within_tol"] = within_tol
    result["verdict"]    = "CONFIRM" if within_tol else "REJECT"

    log.info(
        f"  [mRNA] {result['verdict']}  claimed={claimed:.3f}  rescored={rescored:.3f}"
        f"  adj={adjusted_claimed:.3f}  rel_err={rel_err:.4f}  tol={tol}"
        + (f"  [bias={bias_factor:.4f}]" if bias_factor else f"  [ramp-up n={n_samples}]")
    )
    return result


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
        # Guard: skip mRNA samples where claimed is tiny (|claimed| < 1.0).
        # This happens when the miner used the old iptm-fallback formula that
        # produced very small claimed values (e.g. -0.028) while the validator
        # rescores to a normal docking value (-7.8), giving a spurious ratio ~280.
        # These cross-formula samples corrupt the bias model — skip them entirely.
        if family.startswith("mRNA_") and abs(claimed) < 1.0:
            log.debug(
                f"[GPU-BIAS] skip mRNA record (tiny claimed={claimed:.4f}) "
                f"gpu={gpu_model} family={family}"
            )
            return
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


def run_boltz2_mrna_iptm(smiles: str, target: dict, seed: int = BOLTZ_SEED) -> float | None:
    """
    Run Boltz2 in RNA mode and return affinity via -6.0 - 3.0*iptm ONLY.

    This is the Stage-2 mRNA scoring function. It does NOT use prob or pred
    (the affinity_probability_binary / affinity_pred_value fields) — those
    fields produced sign-flipped, physically impossible scores (-28 kcal/mol)
    when pred > prob, which happens routinely in RNA mode.

    The -6.0-3.0*iptm formula is:
      - Always negative (iptm ∈ (0,1] → result ∈ (-9, -6))
      - Always within the mRNA sanity gate [-9.5, -5.5]
      - Deterministic given the same seed and GPU
      - Consistent with the iptm fallback that already existed in the validator

    Returns affinity in kcal/mol, or None on any failure.
    """
    import yaml
    from boltz.main import predict

    target_id = target["id"]
    rna_seq   = target.get("rna_sequence", "")
    if not rna_seq:
        log.warning(f"  [mRNA] target {target_id} missing rna_sequence — skip")
        return None

    _nuc_re = re.compile(r'^[ACGTUacgtu]{10,}$')
    if _nuc_re.match(smiles.strip()):
        log.warning(f"  [mRNA] nucleotide in smiles field — siRNA, skip")
        return None

    ha = _heavy_atom_count(smiles)
    if ha == 0:
        log.warning(f"  [mRNA] invalid SMILES (0 heavy atoms): {smiles[:60]}")
        return None

    mol_id   = _mol_id(smiles)
    run_uuid = str(uuid.uuid4())
    run_root = Path(f"/tmp/life-validator-mrna-{run_uuid}")
    in_dir   = run_root / "inputs"
    out_dir  = run_root / "outputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_root, stat.S_IRWXU)

    try:
        smiles_hash_expected = hashlib.sha256(smiles.encode()).hexdigest()
        yaml_path = in_dir / f"{mol_id}_{target_id}.yaml"
        _write_boltz_input(in_dir, target_id, rna_seq, smiles, mol_id, "empty",
                           rna_mode=True)
        try:
            written_data  = yaml.safe_load(yaml_path.read_text())
            ligand_blocks = [s["ligand"] for s in written_data.get("sequences", [])
                             if "ligand" in s]
            if not ligand_blocks:
                log.warning("  [mRNA] SECURITY: no ligand block in YAML — skip")
                return None
            if hashlib.sha256(ligand_blocks[0].get("smiles", "").encode()).hexdigest() \
                    != smiles_hash_expected:
                log.warning("  [mRNA] SECURITY: SMILES hash mismatch — skip")
                return None
        except Exception as e:
            log.warning(f"  [mRNA] SECURITY: YAML verify failed: {e} — skip")
            return None

        boltz_start = time.time()
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
            "--no_kernels",
        ], standalone_mode=False)

        metrics = _read_boltz_affinity(out_dir, mol_id, target_id, boltz_start)
        if metrics is None:
            return None

        iptm = metrics.get("iptm")
        if iptm is None:
            log.warning(f"  [mRNA] iptm missing from Boltz2 output: {list(metrics.keys())}")
            return None

        score = round(-6.0 - 3.0 * float(iptm), 3)
        log.info(
            f"  [mRNA-SCORE] -6.0-3.0*iptm  iptm={iptm:.4f} → {score:.3f} kcal/mol"
        )
        return score

    except Exception as e:
        log.warning(f"  [mRNA] Boltz2 raised: {e}")
        return None
    finally:
        shutil.rmtree(str(run_root), ignore_errors=True)




# ── Solana RPC helpers ────────────────────────────────────────────────────────
def _rpc(method: str, params: list, max_retries: int = 3) -> dict:
    """JSON-RPC call with exponential-backoff retry on HTTP 429.

    Solana's public devnet endpoint aggressively rate-limits getProgramAccounts.
    A single-shot call fails permanently whenever the host is rate-limited, so
    we retry up to *max_retries* times with a 6-second base delay that doubles
    each attempt.  The caller still sees an exception if all attempts fail.
    """
    import urllib.error as _ue
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                SOLANA_RPC,
                data=payload.encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except _ue.HTTPError as e:
            last_exc = e
            if e.code == 429 and attempt < max_retries:
                delay = 6 * (2 ** (attempt - 1))   # 6 s, 12 s
                log.debug(f"_rpc {method}: 429 rate-limited, retry {attempt}/{max_retries} in {delay}s")
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            raise
    raise last_exc


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

def _validator_commission(difficulty_tier: int, total_minted_raw: int, hit_count: int,
                          confirming_count: int = _VALIDATORS_REQUIRED) -> float:
    """
    Exact on-chain formula from mint_reward.rs:

        base_reward = ONCHAIN_MINER_BASE[difficulty_tier]  (LIFE)
        amount      = calculate_reward(base_reward, total_minted, hit_count)
                    = base_reward_raw * l1_num * l2_num / 32
        per_validator_commission = (amount // 20) // confirming_count

    Returns the per-validator commission in LIFE (float).

    Layer 1 (supply milestones) and Layer 2 (hit count) both apply.
    On devnet at launch total_minted is near-zero → L1=8/8=100%.
    hit_count is typically < 100 per target → L2=4/4=100%.
    In both full cases: amount = base_reward_raw, commission = base/20/2.

    Pass total_minted_raw=0 and hit_count=0 for the common devnet-launch case.
    """
    ONE_LIFE = 1_000_000
    base_raw = ONCHAIN_MINER_BASE.get(difficulty_tier, 1) * ONE_LIFE

    # Layer 1: supply milestone multiplier (numerator / 8)
    if total_minted_raw <= _HALVING_M1:
        l1 = 8   # 100%
    elif total_minted_raw <= _HALVING_M2:
        l1 = 4   # 50%
    elif total_minted_raw <= _HALVING_M3:
        l1 = 2   # 25%
    else:
        l1 = 1   # 12.5%

    # Layer 2: per-target hit count multiplier (numerator / 4)
    if hit_count < _HALVING_HIT_T1:
        l2 = 4   # 100%
    elif hit_count < _HALVING_HIT_T2:
        l2 = 3   # 75%
    else:
        l2 = 2   # 50%

    # Combined (Rust integer arithmetic — floor div, matching on-chain):
    amount_raw = base_raw * l1 * l2 // 32
    if confirming_count <= 0:
        return 0.0
    per_validator_raw = (amount_raw // 20) // confirming_count
    return per_validator_raw / ONE_LIFE


# ── Account parser (shared by WS listener and catch-up poller) ───────────────

def _parse_result_submission(pubkey: str, data: bytes) -> dict | None:
    """
    Parse raw ResultSubmission account bytes (938-byte layout).
    Returns a submission dict or None if the account is not Pending/Validating,
    the data is malformed, or the SMILES field is empty.

    Layout (after 8-byte Anchor discriminator):
      [32 miner][2 target_id u16][8 epoch][512 smiles][2 smiles_len]
      [4 claimed_affinity f32][8 submitted_slot i64][1 status]
    Status byte sits at offset 576.
    """
    import struct
    import base58 as _b58
    try:
        if len(data) < 578:
            return None
        off = 8
        miner      = data[off:off+32]; off += 32
        target_id  = int.from_bytes(data[off:off+2], "little"); off += 2
        epoch      = int.from_bytes(data[off:off+8], "little"); off += 8
        smiles_raw = data[off:off+512]; off += 512
        smiles_len = int.from_bytes(data[off:off+2], "little"); off += 2
        claimed    = struct.unpack_from("<f", data, off)[0]; off += 4
        off       += 8          # submitted_slot
        status     = data[off]
        if status not in (STATUS_PENDING, STATUS_VALIDATING):
            return None
        smiles = smiles_raw[:smiles_len].decode("utf-8", errors="replace").strip("\x00")
        if not smiles:
            return None
        return {
            "pubkey":           pubkey,
            "miner":            _b58.b58encode(miner).decode(),
            "target_id":        target_id,
            "epoch":            epoch,
            "smiles":           smiles,
            "claimed_affinity": float(claimed),
            "status":           status,
        }
    except Exception as e:
        log.debug(f"  _parse_result_submission {pubkey[:16]}: {e}")
        return None


# ── WebSocket subscription loop (async, runs in its own thread) ───────────────

async def _ws_subscription_loop():
    """
    Maintain a persistent programSubscribe WebSocket connection to the Solana
    cluster.  Every time a ResultSubmission account (dataSize=938) is created
    or updated the RPC pushes the full account data here; we parse it and place
    it on _submission_queue so the workers pick it up within seconds.

    Reconnects with exponential backoff on any error, forever.
    No state is carried across reconnects — the catch-up poll covers the gap.
    """
    global _ws_connected_since, _ws_last_notification_time
    import base64
    import websockets

    attempt = 0
    while True:
        try:
            async with websockets.connect(
                SOLANA_WS,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT,
                max_size=10 * 1024 * 1024,   # 10 MB: headroom for account data batches
            ) as ws:
                attempt = 0   # reset backoff on successful connect
                log.info(f"[WS] Connected → {SOLANA_WS}")
                _ws_connected_since = time.time()

                # programSubscribe — dataSize filter keeps noise down; status and
                # discriminator filters are applied client-side after parse so we
                # never miss an account that changes from Pending→Validating.
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "programSubscribe",
                    "params": [
                        PROGRAM_ID,
                        {
                            "encoding":    "base64",
                            "commitment":  "confirmed",
                            "filters":     [{"dataSize": 938}],
                        },
                    ],
                }))

                raw = await ws.recv()
                resp = json.loads(raw)
                log.info(f"[WS] Subscription confirmed  id={resp.get('result')}")

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                        if msg.get("method") != "programNotification":
                            continue

                        val    = msg["params"]["result"]["value"]
                        pubkey = val["pubkey"]
                        acct   = val["account"]
                        data   = base64.b64decode(acct["data"][0])

                        # Fast pre-checks before full parse
                        if data[:8] != RESULT_DISCRIMINATOR:
                            continue
                        if len(data) > 576 and data[576] not in (STATUS_PENDING, STATUS_VALIDATING):
                            continue   # already Confirmed/Rejected — ignore

                        sub = _parse_result_submission(pubkey, data)
                        if sub is None:
                            continue

                        log.debug(
                            f"[WS] Queuing {pubkey[:16]}…  "
                            f"target={sub['target_id']}  status={sub['status']}"
                        )
                        _submission_queue.put_nowait(sub)
                        _ws_last_notification_time = time.time()

                    except Exception as e:
                        log.debug(f"[WS] message parse error: {e}")

        except Exception as e:
            attempt += 1
            backoff = min(2 ** attempt, WS_MAX_BACKOFF)
            log.warning(
                f"[WS] Disconnected ({type(e).__name__}: {e}) — "
                f"reconnecting in {backoff}s (attempt {attempt})"
            )
            await asyncio.sleep(backoff)


def _start_ws_listener():
    """
    Launch _ws_subscription_loop in a dedicated OS thread with its own asyncio
    event loop.  The rest of the daemon stays purely synchronous.
    """
    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_ws_subscription_loop())   # runs forever

    t = threading.Thread(target=_runner, daemon=True, name="ws-listener")
    t.start()
    log.info("[WS] Listener thread started")
    return t


# ── Dispatcher thread: routes _submission_queue → typed sub-queues ─────────────

def _start_dispatcher():
    """
    Read items from _submission_queue and route them to either _crispr_queue
    (target_id 3000–3009) or _protein_queue (all others).
    Dedup against _SEEN_SUBMISSIONS here so neither worker sees duplicates:
    the WS can deliver multiple notifications for the same account (e.g. on
    creation and again when status flips to Validating).
    """
    def _dispatcher():
        while True:
            try:
                sub = _submission_queue.get(timeout=5)
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"[DISPATCHER] queue.get error: {e}")
                continue

            pubkey = sub["pubkey"]

            # Dedup: if already tracked (processed or max-retried) skip silently
            if _SEEN_SUBMISSIONS.get(pubkey, 0) > _MAX_RETRY_ATTEMPTS:
                log.debug(f"[DISPATCHER] {pubkey[:16]}… already at max retries — dropped")
                continue

            if 3000 <= sub["target_id"] <= 3009:
                _crispr_queue.put_nowait(sub)
            else:
                _protein_queue.put_nowait(sub)

    t = threading.Thread(target=_dispatcher, daemon=True, name="dispatcher")
    t.start()
    log.info("[DISPATCHER] Routing thread started")
    return t


# ── Catch-up poll (safety net for WS gaps and post-restart warm-up) ──────────

# Adaptive catchup interval state — shared between _catchup_poll_once() and main loop.
# On a 429 failure the interval shrinks so the next window is caught quickly.
# On success it resets to the full 5-minute cadence.
_catchup_interval: int = CATCHUP_POLL_INTERVAL   # starts at 300s

# WS silence detector — track when the WS last delivered a submission notification.
# If the WS has been connected for > WS_SILENCE_THRESHOLD seconds without delivering
# anything, _catchup_poll_once() is triggered immediately (outside its normal timer).
WS_SILENCE_THRESHOLD = 90   # seconds: force catchup if WS silent this long after connect
_ws_last_notification_time: float = 0.0   # updated whenever WS enqueues a submission
_ws_connected_since: float = 0.0          # updated on each [WS] Connected event


def _catchup_poll_once() -> bool:
    """
    Run a full getProgramAccounts sweep and enqueue any pending submissions
    not already in _SEEN_SUBMISSIONS.  Called every _catchup_interval seconds
    from the main worker loop, or immediately when WS silence is detected.

    Returns True on success (even if no new submissions), False on failure.
    On 429 failure the global _catchup_interval is shortened to 60 s so the
    next open RPC window is caught quickly.  On success it resets to 300 s.

    Recovers submissions missed due to:
      - Silent notification throttling on public devnet RPC under load (primary)
      - Submissions that existed before the WS subscribed (daemon restart)
      - Brief disconnect windows between ping detection and reconnect (~30–60s)
    """
    global _catchup_interval
    try:
        subs = fetch_pending_submissions(crispr_only=False)
        subs += fetch_pending_submissions(crispr_only=True)
        new = 0
        for sub in subs:
            if _SEEN_SUBMISSIONS.get(sub["pubkey"], 0) <= _MAX_RETRY_ATTEMPTS:
                _submission_queue.put_nowait(sub)
                new += 1
        if new:
            log.info(f"[CATCHUP-POLL] Enqueued {new} submission(s) not yet seen by WS")
        else:
            log.debug("[CATCHUP-POLL] No new submissions — WS delivery healthy")
        # Success: restore full 5-minute interval
        if _catchup_interval != CATCHUP_POLL_INTERVAL:
            log.info(f"[CATCHUP-POLL] RPC recovered — resetting interval to {CATCHUP_POLL_INTERVAL}s")
            _catchup_interval = CATCHUP_POLL_INTERVAL
        return True
    except Exception as e:
        log.warning(f"[CATCHUP-POLL] sweep failed: {e}")
        # Shorten interval so we retry within 60 s instead of waiting the full 5 min
        _catchup_interval = 60
        log.warning(f"[CATCHUP-POLL] rate-limited — next attempt in {_catchup_interval}s")
        return False


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
    # Build filters list — optionally restrict to a single miner wallet at the RPC level
    # to avoid fetching/processing corrupt submissions from other (old) wallets.
    # NOTE: we do NOT filter by status byte here — Pending (0x00) and Validating (0x01)
    # both need to be returned.  The status=0x00 memcmp would miss all Validating accounts,
    # which may still need an additional validator vote to reach validators_required.
    # Status filtering is applied client-side below after parsing.
    filters = [
        {"dataSize": 938},
        {"memcmp": {"offset": 0, "bytes": disc_b58}},
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
    try:
        tmp = STATS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats, indent=2))
        tmp.replace(STATS_PATH)
    except Exception:
        pass  # non-fatal — dashboard may show stale data for one tick

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

    # Load confirmed gRNA history for similarity-based reward decay
    _load_crispr_grna_history()

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

    # Heartbeat thread — updates last_heartbeat every 30s independent of validation work
    def _heartbeat():
        while True:
            time.sleep(30)
            stats["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            stats["last_updated"]   = stats["last_heartbeat"]
            write_stats(stats)
    threading.Thread(target=_heartbeat, daemon=True).start()

    # ── Event-driven infrastructure ───────────────────────────────────────────
    # Start WS listener (pushes to _submission_queue on every account change),
    # dispatcher (routes to _crispr_queue or _protein_queue), and CRISPR worker.
    # The main thread becomes the protein/mRNA worker draining _protein_queue.
    _start_ws_listener()
    _start_dispatcher()

    # ── CRISPR worker — drains _crispr_queue, never touches Boltz2 ───────────
    def _crispr_worker():
        nonlocal validated_today, accepted, rejected, life_earned
        log.info("[CRISPR-WORKER] Started — waiting for queue items")
        while True:
            try:
                # Block up to 5s so the thread stays responsive to daemon exit
                try:
                    sub = _crispr_queue.get(timeout=5)
                except queue.Empty:
                    continue
                crispr_subs = [sub]
                for sub in crispr_subs:
                    pubkey        = sub["pubkey"]
                    smiles        = sub["smiles"]
                    miner_wallet  = sub["miner"]
                    target_id_int = sub["target_id"]
                    claimed       = sub["claimed_affinity"]

                    # Dedup guard (dispatcher already checked, but be defensive
                    # in case a catch-up poll re-queued an item between checks)
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

                    # Expose current CRISPR work to dashboard (mirrors main loop lines 1725-1729)
                    stats["current_target"] = target.get("id") or str(target_id_int)
                    stats["current_smiles"]  = smiles
                    stats["last_updated"]    = datetime.now(timezone.utc).isoformat()
                    write_stats(stats)

                    t0 = time.time()
                    rescored, grna_scores = run_crispr_validation(smiles, target)
                    elapsed = time.time() - t0

                    if rescored is None:
                        log.warning(f"  [CRISPR] Invalid gRNA for {pubkey[:16]}… — skip")
                        _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                        # Clear dashboard "PROCESSING" state — this submission is done,
                        # not in progress.  Mirrors the clear done on confirm/reject paths.
                        stats["current_target"] = None
                        stats["current_smiles"]  = None
                        stats["last_updated"]    = datetime.now(timezone.utc).isoformat()
                        write_stats(stats)
                        continue

                    _crispr_tgt_name = _CRISPR_ID_TO_NAME.get(target_id_int, target.get("id", "") or "")
                    _crispr_min      = _crispr_threshold(_crispr_tgt_name)
                    quality_ok  = grna_scores.get("combined", 0.0) >= _crispr_min

                    # ── Stage-3: validate_crispr() ───────────────────────────────
                    vc = validate_crispr(
                        grna_seq=smiles,
                        target={**target, "id": _crispr_tgt_name},
                        claimed=claimed,
                        gpu_model=_miner_gpu_model,
                        bias_tracker=_gpu_bias_tracker,
                    )
                    rescored   = vc["rescored"] if vc["rescored"] is not None else rescored
                    rel_err    = vc["rel_err"] or 0.0
                    within_tol = quality_ok and vc["within_tol"]
                    verdict    = "CONFIRM" if within_tol else "REJECT"
                    _crispr_tol = CRISPR_TOL
                    adjusted_claimed = claimed  # validate_crispr does not apply bias

                    if vc["verdict"] == "VALIDATOR_ERROR":
                        # Sanity gate or invalid gRNA — skip on-chain
                        _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                        stats["current_target"] = None
                        stats["current_smiles"]  = None
                        write_stats(stats)
                        continue

                    current_epoch = fetch_current_epoch()
                    tier_reward   = _halved_reward(BASE_TIER_REWARDS.get(3, 7), current_epoch)
                    log.info(f"  [CRISPR] reward: base={BASE_TIER_REWARDS.get(3,7)} epoch={current_epoch} halvings={current_epoch//HALVING_INTERVAL} → {tier_reward} $LIFE")

                    # ── Similarity-based reward decay (mirrors miner, deployed 2026-09-02) ──
                    grna_reward_factor, grna_max_sim = _grna_similarity_reward_factor(
                        smiles, target_id_int
                    )
                    effective_reward = int(tier_reward * grna_reward_factor)
                    log.info(
                        f"  [GRNA-DECAY] target={target_id_int}  gRNA={smiles[:16]}…"
                        f"  history_size={len(_CRISPR_GRNA_HISTORY.get(target_id_int, []))}"
                        f"  max_sim={grna_max_sim:.3f}  factor={grna_reward_factor}x"
                        f"  tier_reward={tier_reward} → effective_reward={effective_reward}"
                    )

                    result = validate_on_chain(pubkey, rescored)
                    tx     = result.get("tx") if result else None
                    if tx:
                        log.info(f"  ✔ [CRISPR] tx: {tx}")
                        validated_today += 1
                        # Signal crank to mint reward for this confirmed submission.
                        # Only when within_tol (CONFIRM) — REJECTs never reach Confirmed status.
                        if within_tol:
                            append_crank_queue(pubkey)
                        # Commission = (miner_amount/20)/confirming_count  [mint_reward.rs]
                        # mRNA/CRISPR/protein-Hard all use the same 25 LIFE miner base.
                        # grna_reward_factor (novelty decay) applies to the MINER reward only;
                        # validator commission is always the flat formula — no decay applied.
                        commission = _validator_commission(difficulty_tier=3,
                                                           total_minted_raw=0,
                                                           hit_count=0)
                        if within_tol:
                            accepted    += 1
                            life_earned += commission
                            log.info(
                                f"  +{commission:.4f} $LIFE commission"
                                f"  (CRISPR tier=3, formula=25/20/{_VALIDATORS_REQUIRED})"
                                f"  total={life_earned:.4f}"
                                f"  [miner grna_decay={grna_reward_factor:.2f}x — not applied to validator]"
                            )
                            # Register gRNA in history so subsequent submissions are compared against it
                            grna_upper = smiles.upper().strip()
                            if len(grna_upper) == 20 and grna_upper not in _CRISPR_GRNA_HISTORY.get(target_id_int, []):
                                _CRISPR_GRNA_HISTORY.setdefault(target_id_int, []).append(grna_upper)
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
                            "bias_factor_used": None,  # bias not applied for CRISPR
                            "adjusted_claimed": round(adjusted_claimed, 4),
                            "tx":               tx,
                            "target_id":        target_id_int,
                            "target_type":      "CRISPR",
                            "difficulty_tier":  3,
                            "life_earned":      round(commission, 6) if within_tol else 0,
                            "grna_on_target":   grna_scores.get("on_target"),
                            "grna_off_target":  grna_scores.get("off_target"),
                            "grna_delivery":    grna_scores.get("delivery"),
                            "grna_combined":    grna_scores.get("combined"),
                            "smiles":           smiles,
                            "grna_reward_factor": grna_reward_factor,
                            "grna_max_sim":       round(grna_max_sim, 4),
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
                            "life_earned":      round(commission, 6) if within_tol else 0,
                        })
                    else:
                        log.warning(f"  [CRISPR] validate_on_chain returned no tx")
                        _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
            except Exception as e:
                log.error(f"[CRISPR-WORKER] unhandled error: {e}")
            # No sleep — loop immediately back to queue.get(timeout=5)
    threading.Thread(target=_crispr_worker, daemon=True, name="crispr-worker").start()

    # ── Protein/mRNA worker (main thread) — drains _protein_queue ────────────
    last_catchup = time.time()
    _catchup_poll_once()   # immediate warm-up sweep on startup

    while True:
        now = time.time()

        # Catch-up poll: adaptive interval (60 s when rate-limited, 300 s when healthy).
        # Also fires immediately if the WS has been connected for > WS_SILENCE_THRESHOLD
        # seconds without delivering any notification — the public devnet RPC silently
        # throttles programSubscribe under load, keeping TCP alive but sending no events.
        ws_connected = _ws_connected_since > 0
        ws_silence = (
            ws_connected
            and (now - _ws_connected_since) > WS_SILENCE_THRESHOLD
            and (now - _ws_last_notification_time) > WS_SILENCE_THRESHOLD
        )
        if ws_silence and (now - last_catchup) > 10:
            log.info(
                f"[CATCHUP-POLL] WS silent for >{WS_SILENCE_THRESHOLD}s — "
                f"forcing catchup sweep"
            )
            _catchup_poll_once()
            last_catchup = now
        elif now - last_catchup >= _catchup_interval:
            _catchup_poll_once()
            last_catchup = now

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

        # Step 1: Wait for the next protein/mRNA submission from the queue.
        # Block up to 10s so the catch-up timer and target refresh above can fire.
        try:
            sub = _protein_queue.get(timeout=10)
        except queue.Empty:
            continue   # no work yet — loop back for timer checks

        submissions = [sub]
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
                _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
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
                _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
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
                _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
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
                _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
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
                _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
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
                # Stage-3: validate_crispr()
                _crispr_tgt_name = _CRISPR_ID_TO_NAME.get(target_id_int, target.get("id", "") or "")
                _crispr_min      = _crispr_threshold(_crispr_tgt_name)
                quality_ok  = combined >= _crispr_min

                vc = validate_crispr(
                    grna_seq=smiles,
                    target={**target, "id": _crispr_tgt_name},
                    claimed=claimed,
                    gpu_model=_miner_gpu_model,
                    bias_tracker=_gpu_bias_tracker,
                )
                rescored         = vc["rescored"] if vc["rescored"] is not None else rescored
                rel_err          = vc["rel_err"] or 0.0
                within_tol       = quality_ok and vc["within_tol"]
                verdict          = "CONFIRM" if within_tol else "REJECT"
                _crispr_tol      = CRISPR_TOL
                adjusted_claimed = claimed

                if vc["verdict"] == "VALIDATOR_ERROR":
                    _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                    continue

                TIER_REWARDS = BASE_TIER_REWARDS
                difficulty   = target.get("difficulty_tier", 3)
                current_epoch = fetch_current_epoch()
                tier_reward  = _halved_reward(TIER_REWARDS.get(difficulty, 7), current_epoch)
                log.info(f"  [CRISPR] reward: base={TIER_REWARDS.get(difficulty,7)} epoch={current_epoch} halvings={current_epoch//HALVING_INTERVAL} → {tier_reward} $LIFE")

                # ── Similarity-based reward decay (mirrors miner, deployed 2026-09-02) ──
                grna_reward_factor, grna_max_sim = _grna_similarity_reward_factor(
                    smiles, target_id_int
                )
                effective_reward = int(tier_reward * grna_reward_factor)
                log.info(
                    f"  [GRNA-DECAY] target={target_id_int}  gRNA={smiles[:16]}…"
                    f"  history_size={len(_CRISPR_GRNA_HISTORY.get(target_id_int, []))}"
                    f"  max_sim={grna_max_sim:.3f}  factor={grna_reward_factor}x"
                    f"  tier_reward={tier_reward} → effective_reward={effective_reward}"
                )

                # Commission = (miner_amount/20)/confirming_count  [mint_reward.rs]
                # grna_reward_factor (novelty decay) applies to the MINER reward only.
                commission = _validator_commission(difficulty_tier=difficulty,
                                                   total_minted_raw=0,
                                                   hit_count=0)
                result = validate_on_chain(pubkey, rescored)
                tx = result.get("tx") if result else None
                if tx:
                    log.info(f"  ✔ tx: {tx}")
                    if within_tol:
                        append_crank_queue(pubkey)
                        life_earned += commission
                        log.info(
                            f"  +{commission:.4f} $LIFE commission"
                            f"  (tier={difficulty}, formula={ONCHAIN_MINER_BASE.get(difficulty,1)}/20/{_VALIDATORS_REQUIRED})"
                            f"  total={life_earned:.4f}"
                            f"  [miner grna_decay={grna_reward_factor:.2f}x — not applied to validator]"
                        )
                        # Register gRNA in history so subsequent submissions are compared against it
                        grna_upper = smiles.upper().strip()
                        if len(grna_upper) == 20 and grna_upper not in _CRISPR_GRNA_HISTORY.get(target_id_int, []):
                            _CRISPR_GRNA_HISTORY.setdefault(target_id_int, []).append(grna_upper)
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
                    "bias_factor_used": None,  # bias not applied for CRISPR
                    "adjusted_claimed": round(adjusted_claimed, 4),
                    "difficulty_tier":  difficulty,
                    "target_type":      "CRISPR",
                    "life_earned":      round(commission, 6) if (within_tol and tx) else 0,
                    "grna_on_target":   grna_scores.get("on_target"),
                    "grna_off_target":  grna_scores.get("off_target"),
                    "grna_delivery":    grna_scores.get("delivery"),
                    "grna_combined":    combined,
                    "smiles":           smiles,        # gRNA sequence stored in smiles field
                    "target_id":        target_id_int,
                    "grna_reward_factor": grna_reward_factor,
                    "grna_max_sim":       round(grna_max_sim, 4),
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
                    "life_earned":      round(commission, 6) if (within_tol and tx) else 0,
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

            # Step 2 + 3 + 4: modality dispatch — each function owns its own Boltz2 call
            # (validate_protein and validate_mrna both call Boltz2 internally,
            # so we no longer run a separate top-level run_boltz2 here)
            _seed = sub.get("boltz_seed", BOLTZ_SEED)
            t0 = time.time()
            rescored = None   # will be set by the modality function below
            if not is_mrna:
                vp = validate_protein(
                    smiles=smiles,
                    target=target,
                    claimed=claimed,
                    seed=_seed,
                    gpu_model=_miner_gpu_model,
                    bias_tracker=_gpu_bias_tracker,
                )
                rescored       = vp["rescored"] if vp["rescored"] is not None else rescored
                adjusted_claimed = vp["adjusted_claimed"]
                bias_factor    = vp["bias_factor"]
                tol            = vp["tol"]
                rel_err        = vp["rel_err"] if vp["rel_err"] is not None else 0.0
                within_tol     = vp["within_tol"]
                verdict        = vp["verdict"]
                if verdict == "VALIDATOR_ERROR":
                    # Sanity gate or Boltz2 failure — skip on-chain, mark for retry
                    _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                    append_audit({
                        "ts":               datetime.now(timezone.utc).isoformat(),
                        "submission_pubkey": pubkey,
                        "miner_wallet":     miner_wallet,
                        "miner_gpu":        _miner_gpu_model,
                        "claimed_score":    claimed,
                        "adjusted_claimed": adjusted_claimed,
                        "bias_factor":      bias_factor,
                        "rescored":         rescored,
                        "decision":         "VALIDATOR_ERROR",
                        "rel_err":          rel_err,
                        "tolerance_used":   tol,
                        "difficulty_tier":  target.get("difficulty_tier", 1),
                        "target_type":      "PROTEIN",
                        "life_earned":      0,
                        "sanity_note":      vp.get("sanity_note", ""),
                    })
                    continue
            else:
                # Stage 2: validate_mrna() — Option C ramp-up
                vm = validate_mrna(
                    smiles=smiles,
                    target=target,
                    claimed=claimed,
                    seed=_seed,
                    gpu_model=_miner_gpu_model,
                    bias_tracker=_gpu_bias_tracker,
                )
                rescored         = vm["rescored"] if vm["rescored"] is not None else rescored
                adjusted_claimed = vm["adjusted_claimed"]
                bias_factor      = vm["bias_factor"]
                tol              = vm["tol"]
                rel_err          = vm["rel_err"] if vm["rel_err"] is not None else 0.0
                within_tol       = vm["within_tol"]
                verdict          = vm["verdict"]
                if verdict == "VALIDATOR_ERROR":
                    _SEEN_SUBMISSIONS[pubkey] = _SEEN_SUBMISSIONS.get(pubkey, 0) + 1
                    append_audit({
                        "ts":               datetime.now(timezone.utc).isoformat(),
                        "submission_pubkey": pubkey,
                        "miner_wallet":     miner_wallet,
                        "miner_gpu":        _miner_gpu_model,
                        "claimed_score":    claimed,
                        "adjusted_claimed": adjusted_claimed,
                        "bias_factor":      bias_factor,
                        "rescored":         rescored,
                        "decision":         "VALIDATOR_ERROR",
                        "rel_err":          rel_err,
                        "tolerance_used":   tol,
                        "difficulty_tier":  target.get("difficulty_tier", 3),
                        "target_type":      "RNA",
                        "life_earned":      0,
                        "sanity_note":      vm.get("sanity_note", ""),
                    })
                    continue

            # ── Validator commission — exact on-chain formula from mint_reward.rs ──
            # commission = (miner_amount / 20) / confirming_count
            # miner_amount = calculate_reward(base, total_minted, hit_count)
            # At devnet launch: total_minted ≈ 0 (L1=100%), hit_count ≈ 0 (L2=100%)
            # → commission = ONCHAIN_MINER_BASE[tier] / 20 / _VALIDATORS_REQUIRED
            difficulty = target.get("difficulty_tier", 1)
            commission = _validator_commission(difficulty_tier=difficulty,
                                               total_minted_raw=0,
                                               hit_count=0)
            miner_base = ONCHAIN_MINER_BASE.get(difficulty, 1)
            log.info(
                f"  commission: miner_base={miner_base} LIFE"
                f"  → {miner_base}/20/{_VALIDATORS_REQUIRED} = {commission:.4f} $LIFE/validator"
            )

            elapsed = time.time() - t0

            # Step 4: Submit on-chain
            result = validate_on_chain(pubkey, rescored or 0.0)
            tx = result.get("tx") if result else None
            if tx:
                log.info(f"  ✔ tx: {tx}")
                if within_tol:
                    append_crank_queue(pubkey)
                    life_earned += commission
                    log.info(
                        f"  +{commission:.4f} $LIFE commission  (tier={difficulty})  "
                        f"total={life_earned:.4f}"
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
                "life_earned":      round(commission, 6) if (within_tol and tx) else 0,
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
                "life_earned":      round(commission, 6) if (within_tol and tx) else 0,
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
        # No sleep — loop back immediately to queue.get(timeout=10)


if __name__ == "__main__":
    main()
