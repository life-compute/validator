#!/usr/bin/env python3
"""C44b step 5 — READ-ONLY proof that the anchor comes from the chain and is
compared with the log hash the checker computes itself.

This signs nothing and posts nothing: it runs exactly the two halves the
checker puts together — ChainAnchorBuilder against live devnet, and
LogSource's canonical-log download + SHA-256 — and prints both values side by
side.  No key file is opened.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from http_ua import ua_headers
from payout.batch_hash import day_to_iso
from payout.chain_anchors import ChainAnchorBuilder
from payout.sources import LogSource

COORDINATOR = "https://life-coordinator-devnet.lifecompute313.workers.dev"
OPERATOR = "CZbXxyCmUP4wdcumPXBx3w2RG2dVoWZQUrVw5YsmaNWk"
PROGRAM_ID = "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"


def main(days: list[int]) -> int:
    builder = ChainAnchorBuilder(program_id=PROGRAM_ID,
                                 operator_pubkey=OPERATOR,
                                 cache_path="/tmp/c44b-dry/anchor_cache.json",
                                 headers=ua_headers())
    scan = builder.collect(days)
    print(f"chain scan: {len(scan.anchors)} anchor(s), "
          f"{scan.signatures_seen} signature(s), "
          f"{scan.transactions_fetched} fetched / {scan.cache_hits} cached, "
          f"ignored={scan.ignored}, conflicts={list(scan.conflicts)}")
    log_source = LogSource(COORDINATOR, timeout=20.0, strict_closed=True)
    bad = 0
    for day in days:
        iso = day_to_iso(day)
        rec = scan.anchors.get(day)
        if rec is None:
            print(f"{iso} day={day}  ANCHOR_PENDING (no anchor on chain)")
            continue
        computed = log_source.day_log(day).computed_log_hash
        ok = computed == rec.log_hash
        bad += 0 if ok else 1
        print(f"{iso} day={day}")
        print(f"   chain anchor tx : {rec.signature}")
        print(f"   chain log hash  : {rec.log_hash}")
        print(f"   computed by us  : {computed}")
        print(f"   VERDICT         : {'MATCH' if ok else 'ANCHOR_MISMATCH'}")
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main([int(a) for a in sys.argv[1:]]))
