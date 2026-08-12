# LIFE Compute — Validator Node

> **Help validate cancer drug discoveries. Earn $LIFE tokens.**

Validator nodes re-score molecule submissions from miners using Boltz2 GPU
inference and post confirmations or rejections on-chain. No drug discovery
expertise required — the program handles the science.

## 3-Step Setup

**Step 1 — Install**
```bash
curl -sSL https://raw.githubusercontent.com/life-compute/validator/main/install.sh | bash
```

**Step 2 — Connect wallet**
```bash
~/.life-compute/bin/life-compute-validator wallet connect
```

**Step 3 — Start**
```bash
docker run -d --gpus all --name life-compute-validator \
  -v ~/.life-compute:/root/.life-compute \
  ghcr.io/life-compute/validator:latest
```

## Requirements

| Requirement | Minimum |
|---|---|
| GPU | NVIDIA 8 GB+ VRAM |
| RAM | 16 GB |
| OS | Ubuntu 20.04+ |
| Docker | 20.10+ |
| Solana wallet | Any (Phantom, Solflare, or CLI-generated) |

## How It Works

Every 30 seconds the validator daemon:

1. **Polls Solana** — finds `ResultSubmission` accounts with status `Pending` or `Validating`
2. **Re-runs Boltz2** — scores the miner's SMILES against the same cancer target using GPU inference
3. **Confirms or rejects** — calls `validate_result` on-chain if rescored affinity is within 5% of the miner's claim

Validators earn $LIFE tokens for each confirmed validation.

## PM2 (for local operation)

```bash
git clone https://github.com/life-compute/validator
cd validator
cp .env.example .env   # edit keypair paths
pm2 start ecosystem.config.js
```

Processes:
- `life-validator` — main daemon (port none)
- `life-validator-dashboard` — dashboard on port **3002**

## Dashboard

Open `http://localhost:3002` — four panels:

| Panel | Description |
|---|---|
| Status | ONLINE / OFFLINE |
| Validated Today | submissions processed this session |
| Accept Rate | % within 5% tolerance |
| $LIFE Earned | validator reward accumulator |

## MSA Files

The validator uses the same MSA files as the miner for accurate Boltz2 scoring.
Copy from the miner or download separately:

```bash
cp -r /path/to/life-compute-miner/data/msa_files data/
```

## Configuration

Edit `.env`:

```env
PROGRAM_ID=3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL
SOLANA_RPC=https://api.devnet.solana.com
VALIDATOR_KEYPAIR=~/.life-compute/wallet.json
POLL_SECONDS=30
```

## Related Repos

- [life-compute/miner](https://github.com/life-compute/miner) — GPU miner
- [life-compute/core](https://github.com/life-compute/core) — Solana program
- [life-compute/targets](https://github.com/life-compute/targets) — cancer target database
