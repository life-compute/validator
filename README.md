# LIFE Compute — Validator Node

> **Help validate cancer drug discoveries. Earn $LIFE tokens.**

Validator nodes re-score molecule and gRNA submissions from miners using
Boltz2 GPU inference and post confirmations or rejections on-chain.
No drug discovery expertise required — the program handles the science.

## Requirements

| Requirement | Minimum |
|---|---|
| GPU | NVIDIA 8 GB+ VRAM |
| RAM | 16 GB |
| OS | Ubuntu 20.04+ |
| Python | 3.10+ |
| Node.js | 18+ |
| pm2 | `npm install -g pm2` |
| Solana CLI | [Install](https://docs.solana.com/cli/install-solana-cli-tools) |

---

## Setup (5 steps)

### Step 1 — Clone and install dependencies

```bash
git clone https://github.com/life-compute/validator
cd validator

# Python deps (in a virtualenv)
python3 -m venv .venv && source .venv/bin/activate
pip install boltz==2.2.1 anchorpy==0.20.1 solders==0.21.0 solana==0.34.0 \
            base58==2.1.1 rdkit-pypi pyyaml "requests>=2.32.3"

# Node deps (for registration script and dashboard)
npm install
```

### Step 2 — Create your validator wallet

```bash
solana-keygen new --outfile ~/.life-compute/wallet.json --no-bip39-passphrase
solana config set --keypair ~/.life-compute/wallet.json --url devnet
solana address   # note this — it's your validator public key
```

> ⚠️  Back up `~/.life-compute/wallet.json`. It contains your private key.

### Step 3 — Get devnet SOL

You need SOL to pay for on-chain transactions (registration + validations).

1. Go to **https://faucet.solana.com**
2. Paste your validator public key
3. Request **2 SOL** (enough for registration + hundreds of validations)

Verify balance:
```bash
solana balance
```

### Step 4 — Register as a validator on-chain

This calls `register_validator` on the LIFE Compute program and pays the
0.1 SOL registration fee to the foundation wallet.

```bash
node register_validator.mjs
```

Expected output:
```
Validator pubkey: <your pubkey>
Validator balance: 2.0 SOL
network_config PDA: ...
Current validator_count: N
Calling register_validator...
✓ register_validator succeeded!
  tx: <signature>
  Explorer: https://explorer.solana.com/tx/<sig>?cluster=devnet
```

If it prints `✓ Validator already registered — nothing to do.` you are
already on-chain and can skip to Step 5.

### Step 5 — Configure and start

Copy the example config and fill in your keypair path:

```bash
cp .env.example .env
```

Edit `.env` — at minimum set `VALIDATOR_KEYPAIR` to the path of your wallet:

```env
VALIDATOR_KEYPAIR=~/.life-compute/wallet.json
PAYER_KEYPAIR=~/.life-compute/wallet.json
```

Then start with pm2:

```bash
pm2 start ecosystem.config.js
pm2 save
```

This starts two processes:
- **`life-validator`** — the main daemon (polls every 30 s)
- **`life-validator-dashboard`** — web dashboard on port **3002**

Check it is running:
```bash
pm2 status
pm2 logs life-validator --lines 50
```

---

## Dashboard

Open **http://localhost:3002** in your browser.

| Panel | Description |
|---|---|
| Status | ONLINE / OFFLINE |
| Validated Today | Submissions processed this session |
| Accepted / Rejected | CONFIRM vs REJECT counts |
| $LIFE Earned | Validator reward accumulator |

---

## How It Works

Every `POLL_SECONDS` (default: 30) the validator daemon:

1. **Polls Solana** — finds `ResultSubmission` accounts with status `Pending` or `Validating`
2. **Re-scores** — runs Boltz2 (protein/mRNA) or analytical gRNA scoring (CRISPR) against the same cancer target
3. **Confirms or rejects** — calls `validate_result` on-chain; a submission is confirmed when the rescored affinity is within tolerance of the miner's claim

Validators earn $LIFE tokens for each processed validation.

---

## MSA Files (for accurate Boltz2 scoring)

Boltz2 uses Multiple Sequence Alignment files for protein targets.
Download all 2,000 target MSAs from ColabFold:

```bash
python scripts/download_all_msas.py \
  --targets /tmp/life-compute/targets/targets_2000.json \
  --out-dir data/msa_files/
```

Or copy from an existing miner installation:

```bash
cp -r /path/to/life-compute-miner/data/msa_files data/
```

---

## Configuration (`.env`)

All settings have sensible defaults. See `.env.example` for the full list.

| Variable | Default | Description |
|---|---|---|
| `PROGRAM_ID` | `74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf` | On-chain program address |
| `SOLANA_RPC` | `https://api.devnet.solana.com` | Solana RPC endpoint |
| `VALIDATOR_KEYPAIR` | `~/.life-compute/wallet.json` | Path to validator keypair |
| `PAYER_KEYPAIR` | `~/.life-compute/wallet.json` | Path to fee-payer keypair (usually same) |
| `POLL_SECONDS` | `30` | Polling interval in seconds |
| `TARGETS_URL` | *(targets repo raw JSON)* | Cancer target list URL |
| `MSA_DIR` | `./data/msa_files` | Directory containing `.a3m` MSA files |
| `ANCHOR_DIR` | `/tmp/life-compute/core` | Path to `life-compute/core` checkout (for IDL) |
| `MINER_WALLET` | *(empty — open mode)* | Lock to one miner's wallet, or leave empty to validate all |
| `MINER_GPU_MODEL` | *(auto-detected)* | GPU model string — auto-written by daemon on first run |

---

## Troubleshooting

**`register_validator.mjs` fails with "insufficient funds"**
→ Go back to Step 3 and airdrop more SOL. Registration costs 0.1 SOL plus tx fees.

**Daemon starts but `pm2 logs life-validator` shows no submissions**
→ Normal if no miners are active on devnet. Check `http://localhost:3002` for ONLINE status.

**`Error: Cannot find module '@coral-xyz/anchor'`**
→ Run `npm install` inside the validator directory.

**`boltz` not found / import errors**
→ Activate the venv first: `source .venv/bin/activate`, then re-run `pm2 start`.
→ Or set `interpreter` in `ecosystem.config.js` to your full venv python path (e.g. `.venv/bin/python3`).

---

## Related Repos

- [life-compute/miner](https://github.com/life-compute/miner) — GPU miner
- [life-compute/core](https://github.com/life-compute/core) — Solana program
- [life-compute/targets](https://github.com/life-compute/targets) — Cancer target database
