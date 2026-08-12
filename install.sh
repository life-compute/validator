#!/usr/bin/env bash
# ============================================================
#  LIFE Compute Validator Installer
#  Help validate cancer drug discoveries. Earn $LIFE tokens.
# ============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; MAGENTA='\033[0;35m'; BOLD='\033[1m'
DIM='\033[2m'; RESET='\033[0m'

LIFE_DIR="$HOME/.life-compute"
DOCKER_IMAGE="ghcr.io/life-compute/validator:latest"
SERVICE_NAME="life-compute-validator"

info()    { echo -e "${CYAN}  ℹ  ${RESET}$*"; }
success() { echo -e "${GREEN}  ✔  ${RESET}$*"; }
warn()    { echo -e "${YELLOW}  ⚠  ${RESET}$*"; }
die()     { echo -e "${RED}  ✖  ${RESET}$*" >&2; exit 1; }

step() {
  echo ""
  echo -e "${BOLD}${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${BOLD}${MAGENTA}  Step $1: $2${RESET}"
  echo -e "${BOLD}${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

echo ""
echo -e "${BOLD}${CYAN}  ✦  LIFE Compute Validator  ✦${RESET}"
echo -e "${DIM}     Help validate cancer drug discoveries. Earn \$LIFE tokens.${RESET}"
echo ""
echo -e "${DIM}  Installer 1.0.0  •  $(date '+%Y-%m-%d')${RESET}"
echo ""

# ════════════════════════════════════════════════════════════
step 1 "Download & Prerequisites"
# ════════════════════════════════════════════════════════════

command -v docker &>/dev/null || die "Docker not found. Install from https://docs.docker.com/get-docker/"
docker info &>/dev/null 2>&1   || die "Docker daemon not running."
success "Docker $(docker --version | grep -oP '\d+\.\d+' | head -1) found"

GPU_OK=false
if command -v nvidia-smi &>/dev/null; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "Unknown")
  success "NVIDIA GPU: ${GPU_NAME}"
  GPU_OK=true
else
  warn "No NVIDIA GPU detected — validator will run slowly in CPU mode"
fi

info "Pulling ${DOCKER_IMAGE}..."
if docker pull "${DOCKER_IMAGE}" 2>/dev/null; then
  success "Image pulled"
else
  warn "Image not yet in registry — build locally with: docker build -t ${DOCKER_IMAGE} ."
fi

mkdir -p "${LIFE_DIR}"
success "Config dir: ${LIFE_DIR}"

# ════════════════════════════════════════════════════════════
step 2 "Connect Your Solana Wallet"
# ════════════════════════════════════════════════════════════

# (mirrors miner install.sh wallet step exactly)
WALLET_FILE="${LIFE_DIR}/wallet.json"
mkdir -p "${LIFE_DIR}/bin"

# Install the wallet CLI helper
cat > "${LIFE_DIR}/bin/life-compute-validator" << 'CLISCRIPT'
#!/usr/bin/env bash
# life-compute-validator CLI helper
LIFE_DIR="$HOME/.life-compute"
WALLET_FILE="${LIFE_DIR}/wallet.json"
cmd="${1:-help}"

if [[ "$cmd" == "wallet" && "${2:-}" == "connect" ]]; then
  echo ""
  echo "  How would you like to set up your wallet?"
  echo "    [1] Enter existing Solana address"
  echo "    [2] Generate new keypair (recommended)"
  read -rp "  Choice [2]: " c; c="${c:-2}"

  if [[ "$c" == "1" ]]; then
    read -rp "  Solana wallet address: " addr
    echo "{\"pubkey\":\"${addr}\",\"type\":\"provided\"}" > "${WALLET_FILE}"
    echo "  ✔ Saved: ${addr}"
  else
    if command -v solana-keygen &>/dev/null; then
      solana-keygen new --outfile "${WALLET_FILE}" --no-bip39-passphrase --force
      echo "  ✔ Keypair: $(solana-keygen pubkey "${WALLET_FILE}")"
    else
      python3 -c "
import json, secrets
kp = list(secrets.token_bytes(64))
with open('${WALLET_FILE}', 'w') as f: json.dump(kp, f)
print('  ✔ Keypair generated (install Solana CLI for a proper one)')
"
    fi
    echo "  ⚠  Back up ${WALLET_FILE} — it contains your private key"
  fi
  exit 0
fi

echo "  LIFE Compute Validator CLI"
echo "  Usage: life-compute-validator wallet connect"
CLISCRIPT
chmod +x "${LIFE_DIR}/bin/life-compute-validator"

# Add to PATH if not already there
if ! echo "$PATH" | grep -q "${LIFE_DIR}/bin"; then
  echo "export PATH=\"\$PATH:${LIFE_DIR}/bin\"" >> "${HOME}/.bashrc" 2>/dev/null || true
  echo "export PATH=\"\$PATH:${LIFE_DIR}/bin\"" >> "${HOME}/.zshrc"  2>/dev/null || true
  info "Added ${LIFE_DIR}/bin to PATH (restart shell or: export PATH=\$PATH:${LIFE_DIR}/bin)"
fi
success "CLI installed: ${LIFE_DIR}/bin/life-compute-validator"
info "Run: ~/.life-compute/bin/life-compute-validator wallet connect"

# ════════════════════════════════════════════════════════════
step 3 "Start Validator"
# ════════════════════════════════════════════════════════════

GPU_FLAG=""
[[ "$GPU_OK" == "true" ]] && GPU_FLAG="--gpus all"

DOCKER_RUN_CMD="docker run -d --name ${SERVICE_NAME} --restart unless-stopped ${GPU_FLAG} -v ${LIFE_DIR}:/root/.life-compute ${DOCKER_IMAGE}"

docker rm -f "${SERVICE_NAME}" &>/dev/null || true
if eval "${DOCKER_RUN_CMD}" 2>/dev/null; then
  success "Validator started (name: ${SERVICE_NAME})"
else
  warn "Could not start container. Once image is available:"
  echo -e "  ${CYAN}${DOCKER_RUN_CMD}${RESET}"
fi

echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}${GREEN}  ✦  Installation Complete!  ✦${RESET}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  ${CYAN}Logs:${RESET}       docker logs -f ${SERVICE_NAME}"
echo -e "  ${CYAN}Dashboard:${RESET}  http://localhost:3002"
echo -e "  ${CYAN}Config:${RESET}     ${LIFE_DIR}"
echo ""
