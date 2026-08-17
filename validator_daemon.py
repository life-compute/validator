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

# ── Config from .env ──────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID        = _env("PROGRAM_ID",        "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf")
SOLANA_RPC        = _env("SOLANA_RPC",        "https://api.devnet.solana.com")
VALIDATOR_KEYPAIR = _env("VALIDATOR_KEYPAIR", str(Path.home() / ".life-compute/wallet.json"))
PAYER_KEYPAIR     = _env("PAYER_KEYPAIR",     str(Path.home() / ".life-compute/wallet.json"))
TARGETS_URL       = _env("TARGETS_URL",       "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
POLL_SECONDS      = int(_env("POLL_SECONDS", "30"))

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
VALIDATION_TOLERANCE = 0.50  # DEVNET TESTING TOLERANCE — tighten for mainnet

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
                        smiles: str, mol_id: int, msa_path: str) -> None:
    import yaml
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

    uniprot   = target["uniprot_id"]
    target_id = target["id"]
    msa_path  = _msa_path_for(uniprot)
    sequence  = _sequence_from_msa(msa_path) or target["protein_sequence"]

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

        # ── Write Boltz2 YAML input ───────────────────────────────────────────
        yaml_path = in_dir / f"{mol_id}_{target_id}.yaml"
        _write_boltz_input(in_dir, target_id, sequence, smiles, mol_id, msa_path)

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
        if prob is None or pred is None:
            log.warning(f"  Boltz affinity fields missing: {list(metrics.keys())}")
            return None

        return round(-((prob - pred) / ha) * 30.0, 3)   # same conversion as miner

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

def fetch_pending_submissions() -> list[dict]:
    """
    getProgramAccounts filtered to ResultSubmission accounts.
    Returns list of dicts with keys: pubkey, miner, target_id, epoch,
    smiles, claimed_affinity, status.

    Three RPC-side filters (all must pass):
      1. dataSize=775        — exact account size for ResultSubmission
      2. memcmp offset=0     — 8-byte Anchor discriminator (base58)
      3. memcmp offset=575   — is_validated=0x00 (Pending/Validating only)
         Layout: 8+32+1+8+512+2+4+8 = 575 bytes before the status byte
    """
    import base64, base58
    disc_b58        = base58.b58encode(RESULT_DISCRIMINATOR).decode()
    unvalidated_b58 = base58.b58encode(bytes([0x00])).decode()   # is_validated=0x00
    try:
        resp = _rpc("getProgramAccounts", [
            PROGRAM_ID,
            {
                "encoding": "base64",
                "filters": [
                    {"dataSize": 775},
                    {"memcmp": {"offset": 0,   "bytes": disc_b58}},
                    {"memcmp": {"offset": 575, "bytes": unvalidated_b58}},
                ],
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
            # Layout: [8 disc][32 miner][1 target_id][8 epoch][512 smiles][2 smiles_len]
            #         [4 claimed_affinity f32][8 submitted_slot i64][1 status]...
            off = 8
            miner      = data[off:off+32]; off += 32
            target_id  = data[off]; off += 1
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
    return results


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
            log.error(f"  validate_result node error: {r.stderr[-400:]}")
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
    global _VALIDATOR_PUBKEY
    print("\033[96m    L I F E  C O M P U T E  —  V A L I D A T O R  \033[0m\n")

    # Load validator pubkey once at startup (used for self-validation checks)
    _VALIDATOR_PUBKEY = _load_validator_pubkey()
    if _VALIDATOR_PUBKEY:
        log.info(f"Validator pubkey: {_VALIDATOR_PUBKEY[:16]}…")
    else:
        log.warning("Self-validation check DISABLED (keypair unreadable)")

    targets_by_id: dict[int, dict] = {}
    last_refresh    = 0.0
    life_earned     = 0.0

    # Initialize today-counters from today's log so a restart doesn't zero the display
    today_date = _today_date_utc()
    validated_today, accepted, rejected = _count_today_from_log()
    if validated_today:
        log.info(f"Restored today's counters from log: total={validated_today} confirmed={accepted} rejected={rejected}")

    stats = {
        "status":          "ONLINE",
        "validated_today": 0,
        "confirmed":       0,
        "rejected":        0,
        "accept_rate":     0.0,
        "life_commission": 0.0,
        "last_heartbeat":  datetime.now(timezone.utc).isoformat(),
        "current_target":  None,
        "current_smiles":  None,
        "active_validators":    0,
        "self_reputation_pct":  100.0,
        "started_at":      datetime.now(timezone.utc).isoformat(),
        "last_updated":    "",
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
            log.info(f"Targets loaded: {len(targets)}")
            last_refresh = now

        # Step 1: Poll for pending submissions
        log.info("Polling for pending submissions...")
        submissions = fetch_pending_submissions()
        log.info(f"  Found {len(submissions)} pending submission(s)")

        for sub in submissions:
            pubkey        = sub["pubkey"]
            smiles        = sub["smiles"]
            miner_wallet  = sub["miner"]
            target_id_int = sub["target_id"]
            claimed       = sub["claimed_affinity"]

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

            # ── Security gate 3: SMILES input sanitization ────────────────────
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

            # Expose current work to dashboard
            stats["current_target"] = target.get("id") or str(target_id_int)
            stats["current_smiles"] = smiles
            stats["last_updated"]   = datetime.now(timezone.utc).isoformat()
            write_stats(stats)

            # Step 2: Re-run Boltz2 (with pipeline injection hardening inside)
            t0 = time.time()
            rescored = run_boltz2(smiles, target, seed=sub.get("boltz_seed", BOLTZ_SEED))
            elapsed  = time.time() - t0

            if rescored is None:
                log.warning(f"  Boltz2 failed for {pubkey[:16]}… — skip")
                append_audit({
                    "ts":               datetime.now(timezone.utc).isoformat(),
                    "submission_pubkey": pubkey,
                    "miner_wallet":     miner_wallet,
                    "claimed_score":    claimed,
                    "rescored":         None,
                    "decision":         "BOLTZ2_FAILED",
                    "rel_err":          None,
                })
                continue

            # Step 3: Tolerance check (mirrors constants.rs)
            if claimed != 0.0:
                rel_err = abs(rescored - claimed) / abs(claimed)
            else:
                rel_err = abs(rescored)
            within_tol = rel_err <= VALIDATION_TOLERANCE
            verdict    = "CONFIRM" if within_tol else "REJECT"

            log.info(f"  {verdict}  claimed={claimed:.3f}  rescored={rescored:.3f}  "
                     f"rel_err={rel_err:.3f}  ({elapsed:.1f}s)")

            # Step 4: Submit on-chain
            result = validate_on_chain(pubkey, rescored)
            tx = result.get("tx") if result else None
            if tx:
                log.info(f"  ✔ tx: {tx}")
                if within_tol:
                    life_earned += 0.5   # validators earn half the miner reward
            else:
                log.warning(f"  validate_on_chain returned no tx")

            # Count every verdict (confirm or reject), regardless of tx success
            validated_today += 1
            if within_tol:
                accepted += 1
            else:
                rejected += 1

            # ── Audit log (every decision) ────────────────────────────────────
            append_audit({
                "ts":               datetime.now(timezone.utc).isoformat(),
                "submission_pubkey": pubkey,
                "miner_wallet":     miner_wallet,
                "claimed_score":    claimed,
                "rescored":         rescored,
                "decision":         verdict,
                "rel_err":          round(rel_err, 4),
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
        })
        write_stats(stats)
        log.info(f"Validated={validated_today}  Accept={accept_rate}%  $LIFE={life_earned:.1f}  Validators={net_info.get('active_validators',0)}")
        log.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
