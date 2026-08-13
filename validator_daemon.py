#!/usr/bin/env python3
"""
LIFE Compute — Validator Daemon (devnet)

Three-step loop every POLL_SECONDS:
  1. getProgramAccounts → find all ResultSubmission PDAs with status Pending/Validating
  2. For each: re-run Boltz2 on the claimed SMILES against the same target
  3. Call validate_result on-chain — confirm if |rescored - claimed| / |claimed| ≤ 5%

On-chain call uses the same Node.js / Anchor stack as the miner.
Boltz2 scoring runs directly via the boltz Python API (pip boltz==2.2.1),
Results logged to output/validator_log.jsonl and stats.json for the dashboard.
"""
import json, time, logging, os, subprocess, sys, urllib.request, tempfile
from pathlib import Path
from datetime import datetime, timezone

# ── Config from .env ──────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID       = _env("PROGRAM_ID",       "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf")
SOLANA_RPC       = _env("SOLANA_RPC",       "https://api.devnet.solana.com")
VALIDATOR_KEYPAIR = _env("VALIDATOR_KEYPAIR", str(Path.home() / ".life-compute/wallet.json"))
PAYER_KEYPAIR    = _env("PAYER_KEYPAIR",    str(Path.home() / ".life-compute/wallet.json"))
TARGETS_URL      = _env("TARGETS_URL",      "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
POLL_SECONDS     = int(_env("POLL_SECONDS", "30"))

WORK_DIR   = Path(__file__).parent
STATS_PATH = WORK_DIR / "stats.json"
LOG_JSONL  = WORK_DIR / "output" / "validator_log.jsonl"
(WORK_DIR / "output").mkdir(exist_ok=True)

# ── Boltz2 / MSA paths ────────────────────────────────────────────────────────
MSA_DIR    = Path(_env("MSA_DIR", str(WORK_DIR / "data" / "msa_files")))
_BOLTZ_TMP = Path(tempfile.gettempdir()) / "life-validator-boltz"

# Fast inference settings (match miner)
_RECYCLING_STEPS       = 1
_SAMPLING_STEPS        = 25
_DIFFUSION_SAMPLES     = 1
_SAMPLING_STEPS_AFF    = 25
_DIFFUSION_SAMPLES_AFF = 1

# ── Anchor / JS paths ─────────────────────────────────────────────────────────
ANCHOR_DIR  = Path(_env("ANCHOR_DIR", "/tmp/life-compute/core"))
IDL_PATH    = ANCHOR_DIR / "target/idl/life_core.json"
VALIDATE_JS = WORK_DIR / "life_validate.js"

# ── ResultSubmission discriminator (from IDL) ─────────────────────────────────
RESULT_DISCRIMINATOR = bytes([214, 115, 165, 103, 67, 211, 47, 88])
STATUS_PENDING    = 0   # Pending
STATUS_VALIDATING = 1   # Validating

BOLTZ_SEED = 68  # must match miner BOLTZ_SEED; used for reproducible Boltz2 rescoring
VALIDATION_TOLERANCE = 0.25  # DEVNET TESTING TOLERANCE — tighten for mainnet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-validator")


# ── Boltz2 scoring — self-contained, no nova/miner dependencies ───────────────

def _mol_id(smiles: str) -> int:
    import hashlib
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


def _read_boltz_affinity(out_dir: Path, mol_id: int, target_id: str) -> dict | None:
    pred_dir = out_dir / "boltz_results_inputs" / "predictions" / f"{mol_id}_{target_id}"
    if not pred_dir.exists():
        log.warning(f"  Boltz output dir missing: {pred_dir}")
        return None
    combined = {}
    for fp in pred_dir.iterdir():
        if fp.name.startswith(("affinity", "confidence")):
            try:
                combined.update(json.loads(fp.read_text()))
            except Exception as e:
                log.warning(f"  Could not parse {fp.name}: {e}")
    return combined or None


def run_boltz2(smiles: str, target: dict, seed: int = BOLTZ_SEED) -> float | None:
    """Re-score a SMILES via Boltz2. Returns affinity in kcal/mol or None.
    seed must match the seed used by the submitting miner for reproducible scores."""
    import shutil
    from boltz.main import predict

    uniprot   = target["uniprot_id"]
    target_id = target["id"]
    msa_path  = _msa_path_for(uniprot)
    sequence  = _sequence_from_msa(msa_path) or target["protein_sequence"]

    ha = _heavy_atom_count(smiles)
    if ha == 0:
        log.warning(f"  Invalid SMILES (0 heavy atoms): {smiles[:60]}")
        return None

    mol_id  = _mol_id(smiles)
    batch   = f"batch_{int(time.time()*1000)}"
    in_dir  = _BOLTZ_TMP / batch / "inputs"
    out_dir = _BOLTZ_TMP / batch / "outputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        _write_boltz_input(in_dir, target_id, sequence, smiles, mol_id, msa_path)
        # predict is a Click command in boltz 2.2.1 — must use .main() not direct call.
        # Direct call hits Click's Context.__init__(data=...) which fails.
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
        metrics = _read_boltz_affinity(out_dir, mol_id, target_id)
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
        shutil.rmtree(str(_BOLTZ_TMP / batch), ignore_errors=True)


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
    disc_b58      = base58.b58encode(RESULT_DISCRIMINATOR).decode()
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
            smiles = smiles_raw[:smiles_len].decode("utf-8", errors="replace").strip("\x00")
            if not smiles:
                continue
            results.append({
                "pubkey":           pubkey,
                "miner":            base58.b58encode(miner).decode(),
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
    STATS_PATH.write_text(json.dumps(stats, indent=2))

def append_log(row: dict):
    with LOG_JSONL.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    print("\033[96m    L I F E  C O M P U T E  —  V A L I D A T O R  \033[0m\n")

    targets_by_id: dict[int, dict] = {}
    last_refresh   = 0.0
    validated_today = 0
    accepted        = 0
    rejected        = 0
    life_earned     = 0.0

    stats = {
        "status": "ONLINE",
        "validated_today": 0, "accepted": 0, "rejected": 0,
        "accept_rate": 0.0, "life_earned": 0.0,
        "started_at": datetime.now(timezone.utc).isoformat(), "last_updated": "",
    }
    write_stats(stats)

    while True:
        now = time.time()

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
            target_id_int = sub["target_id"]
            claimed       = sub["claimed_affinity"]

            target = targets_by_id.get(target_id_int)
            if not target:
                log.warning(f"  {pubkey[:16]}…: unknown target_id={target_id_int} — skip")
                continue

            log.info(f"  Validating {pubkey[:16]}…  target={target.get('id','?')}  "
                     f"claimed={claimed:.3f}  smiles={smiles[:40]}")

            # Step 2: Re-run Boltz2
            t0 = time.time()
            rescored = run_boltz2(smiles, target, seed=sub.get("boltz_seed", BOLTZ_SEED))
            elapsed  = time.time() - t0

            if rescored is None:
                log.warning(f"  Boltz2 failed for {pubkey[:16]}… — skip")
                continue

            # Step 3: Tolerance check (mirrors constants.rs)
            if claimed != 0.0:
                rel_err = abs(rescored - claimed) / abs(claimed)
            else:
                rel_err = abs(rescored)
            within_tol = rel_err <= VALIDATION_TOLERANCE
            verdict = "CONFIRM" if within_tol else "REJECT"

            log.info(f"  {verdict}  claimed={claimed:.3f}  rescored={rescored:.3f}  "
                     f"rel_err={rel_err:.3f}  ({elapsed:.1f}s)")

            # Step 3: Submit on-chain
            result = validate_on_chain(pubkey, rescored)
            tx = result.get("tx") if result else None
            if tx:
                log.info(f"  ✔ tx: {tx}")
                if within_tol:
                    accepted   += 1
                    life_earned += 0.5   # validators earn half the miner reward
                else:
                    rejected += 1
            else:
                log.warning(f"  validate_on_chain returned no tx")

            validated_today += 1
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

        accept_rate = round(accepted / max(validated_today, 1) * 100, 1)
        stats.update({
            "status": "ONLINE",
            "validated_today": validated_today,
            "accepted": accepted,
            "rejected": rejected,
            "accept_rate": accept_rate,
            "life_earned": life_earned,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })
        write_stats(stats)
        log.info(f"Validated={validated_today}  Accept={accept_rate}%  $LIFE={life_earned:.1f}")
        log.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
