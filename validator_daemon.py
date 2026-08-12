#!/usr/bin/env python3
"""
LIFE Compute — Validator Daemon (devnet)

Three-step loop every POLL_SECONDS:
  1. getProgramAccounts → find all ResultSubmission PDAs with status Pending/Validating
  2. For each: re-run Boltz2 on the claimed SMILES against the same target
  3. Call validate_result on-chain — confirm if |rescored - claimed| / |claimed| ≤ 5%

On-chain call uses the same Node.js / Anchor stack as the miner.
Boltz2 scoring mirrors miner_daemon.py exactly (nova venv subprocess).
Results logged to output/validator_log.jsonl and stats.json for the dashboard.
"""
import json, time, logging, os, subprocess, sys, urllib.request, tempfile
from pathlib import Path
from datetime import datetime, timezone

# ── Config from .env ──────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID       = _env("PROGRAM_ID",       "3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL")
SOLANA_RPC       = _env("SOLANA_RPC",       "https://api.devnet.solana.com")
VALIDATOR_KEYPAIR = _env("VALIDATOR_KEYPAIR", str(Path.home() / ".life-compute/wallet.json"))
PAYER_KEYPAIR    = _env("PAYER_KEYPAIR",    str(Path.home() / ".life-compute/wallet.json"))
TARGETS_URL      = _env("TARGETS_URL",      "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
POLL_SECONDS     = int(_env("POLL_SECONDS", "30"))

WORK_DIR   = Path(__file__).parent
STATS_PATH = WORK_DIR / "stats.json"
LOG_JSONL  = WORK_DIR / "output" / "validator_log.jsonl"
(WORK_DIR / "output").mkdir(exist_ok=True)

# ── Boltz2 / nova paths (same as miner) ───────────────────────────────────────
NOVA_DIR  = Path(_env("NOVA_DIR", "/mnt/minos-drive/nova_subnet"))
NOVA_VENV = NOVA_DIR / ".venv" / "bin" / "python"
MSA_DIR   = Path(_env("MSA_DIR", str(WORK_DIR / "data" / "msa_files")))

# ── Anchor / JS paths ─────────────────────────────────────────────────────────
ANCHOR_DIR  = Path(_env("ANCHOR_DIR", "/tmp/life-compute/core"))
IDL_PATH    = ANCHOR_DIR / "target/idl/life_core.json"
VALIDATE_JS = WORK_DIR / "life_validate.js"

# ── ResultSubmission discriminator (from IDL) ─────────────────────────────────
RESULT_DISCRIMINATOR = bytes([214, 115, 165, 103, 67, 211, 47, 88])
STATUS_PENDING    = 0   # Pending
STATUS_VALIDATING = 1   # Validating

VALIDATION_TOLERANCE = 0.05  # 5% — must match constants.rs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-validator")


# ── Boltz2 scoring (mirrors miner_daemon.py exactly) ─────────────────────────
_BOLTZ_HELPER = """\
import sys, json
sys.path.insert(0, "{nova_dir}")

from nova_adaptive.nova_pulse_scorer import score_batch
from pathlib import Path

args      = json.loads(sys.argv[1])
smiles    = args["smiles"]
target_id = args["target_id"]
msa_path  = args["msa_path"]
sequence  = args["sequence"]

scores = score_batch([smiles], target_id, sequence, msa_path)
print(json.dumps({{"boltz_score": scores.get(smiles), "smiles": smiles, "target_id": target_id}}))
"""

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
                if line and not line.startswith("#") and not line.startswith(">"):
                    return line
    except OSError:
        pass
    return None

def run_boltz2(smiles: str, target: dict) -> float | None:
    """Re-score a SMILES via Boltz2. Returns affinity in kcal/mol or None."""
    uniprot  = target["uniprot_id"]
    msa_path = _msa_path_for(uniprot)
    sequence = _sequence_from_msa(msa_path) or target["protein_sequence"]

    helper_src = _BOLTZ_HELPER.format(nova_dir=str(NOVA_DIR))
    args_json  = json.dumps({
        "smiles": smiles, "target_id": target["id"],
        "msa_path": msa_path, "sequence": sequence,
    })
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                     prefix="hermes-boltz-val-") as f:
        f.write(helper_src); helper_path = f.name
    try:
        r = subprocess.run(
            [str(NOVA_VENV), helper_path, args_json],
            capture_output=True, text=True, timeout=300, cwd=str(NOVA_DIR),
        )
    finally:
        os.unlink(helper_path)

    if r.returncode != 0:
        log.warning(f"  Boltz2 err: {r.stderr[-300:]}")
        return None
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            d = json.loads(line)
            bs = d.get("boltz_score")
            if bs is not None:
                return round(-float(bs) * 30.0, 3)  # same conversion as miner
        except Exception:
            continue
    return None


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
    getProgramAccounts filtered to ResultSubmission discriminator.
    Returns list of dicts with keys: pubkey, miner, target_id, epoch,
    smiles, claimed_affinity, status.
    """
    import base64
    disc_b64 = base64.b64encode(RESULT_DISCRIMINATOR).decode()
    try:
        resp = _rpc("getProgramAccounts", [
            PROGRAM_ID,
            {
                "encoding": "base64",
                "filters": [
                    {"memcmp": {"offset": 0, "bytes": base64.b64encode(RESULT_DISCRIMINATOR).decode()}},
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
            import base58
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
            rescored = run_boltz2(smiles, target)
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
