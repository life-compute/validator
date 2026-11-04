#!/usr/bin/env bash
# C44b step 5 — dry run in the WORKTREE against the live devnet.
#
# Temporary copies only: PAYOUT_STATE_DIR, PAYOUT_WORK_DIR and PAYOUT_ANCHORS
# all point into /tmp/c44b-dry/<day>, so the real ledger
# (/root/scratch/validator_c2/payout_state) and /root/payout-work are never
# written.  The Worker is read-only here: the checker posts nothing because
# every day in question is already in the copied ledger.
set -uo pipefail
cd /root/scratch/validator_c2_anchors

DAY="$1"
ROOT=/tmp/c44b-dry/"$DAY"
rm -rf "$ROOT"; mkdir -p "$ROOT/work"
cp -a /root/scratch/validator_c2/payout_state "$ROOT/state"

sed -e "s#^PAYOUT_STATE_DIR=.*#PAYOUT_STATE_DIR=$ROOT/state#" \
    -e "s#^PAYOUT_WORK_DIR=.*#PAYOUT_WORK_DIR=$ROOT/work#" \
    -e "s#^PAYOUT_ANCHORS=.*#PAYOUT_ANCHORS=$ROOT/work/anchors.json#" \
    -e "s#^ANCHOR_SOURCE=.*#ANCHOR_OPERATOR_PUBKEY=CZbXxyCmUP4wdcumPXBx3w2RG2dVoWZQUrVw5YsmaNWk#" \
    -e "s#^PAYOUT_FIRST_DAY=.*#PAYOUT_FIRST_DAY=$DAY#" \
    /root/payout-work/service.env > "$ROOT/service.env"
echo "PAYOUT_LAST_DAY=$DAY" >> "$ROOT/service.env"
chmod 600 "$ROOT/service.env"

/root/life-validator/.venv/bin/python3 payout_checker_runner.py \
    --config "$ROOT/service.env"
rc=$?
echo "--- exit=$rc ---"
exit $rc
