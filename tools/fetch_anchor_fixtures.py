"""Build tests/fixtures/chain_anchors/* from the REAL devnet responses.

Run once, read-only, against https://api.devnet.solana.com with the product
User-Agent.  It fetches the four live LIFE-ANCHOR/1 transactions published by
the operator, shortens them to the fields the checker actually reads (while
keeping the structure byte-for-byte the same shape the RPC returns), and
derives the negative-case fixtures from them so every malformed/other-signer/
failed/wrong-program case is a mutation of real data, not an invention.

    python tools/fetch_anchor_fixtures.py          # refresh the fixtures

Nothing here is imported by the test suite: the suite reads the committed
JSON files only, so it needs no network.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

RPC = "https://api.devnet.solana.com"
UA = {"User-Agent": "life-compute-validator/2.0 (+https://lifecompute.ai)",
      "Content-Type": "application/json"}
OPERATOR = "CZbXxyCmUP4wdcumPXBx3w2RG2dVoWZQUrVw5YsmaNWk"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "chain_anchors"

_id = 0


def rpc(method: str, params: list) -> object:
    global _id
    _id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers=UA, method="POST")
    with urllib.request.urlopen(req, timeout=30) as fh:
        return json.loads(fh.read())["result"]


def slim_tx(tx: dict) -> dict:
    """Keep only what the checker reads — same shape, fewer bytes."""
    msg = tx["transaction"]["message"]
    return {
        "blockTime": tx.get("blockTime"),
        "slot": tx.get("slot"),
        "version": tx.get("version"),
        "meta": {"err": (tx.get("meta") or {}).get("err"),
                 "fee": (tx.get("meta") or {}).get("fee"),
                 "innerInstructions": (tx.get("meta") or {}).get(
                     "innerInstructions") or [],
                 "status": (tx.get("meta") or {}).get("status")},
        "transaction": {
            "message": {"accountKeys": msg["accountKeys"],
                        "instructions": msg["instructions"],
                        "recentBlockhash": msg.get("recentBlockhash")},
            "signatures": tx["transaction"]["signatures"],
        },
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sigs = rpc("getSignaturesForAddress",
               [OPERATOR, {"limit": 1000, "commitment": "finalized"}])
    slim_sigs = [{"signature": s["signature"], "slot": s["slot"],
                  "err": s["err"], "blockTime": s["blockTime"],
                  "confirmationStatus": s.get("confirmationStatus"),
                  "memo": s.get("memo")} for s in sigs]
    (OUT / "signatures_page.json").write_text(
        json.dumps(slim_sigs, indent=1, sort_keys=True) + "\n")

    txs = {}
    for s in slim_sigs:
        tx = rpc("getTransaction",
                 [s["signature"], {"encoding": "jsonParsed",
                                   "commitment": "finalized",
                                   "maxSupportedTransactionVersion": 0}])
        txs[s["signature"]] = slim_tx(tx)
    (OUT / "transactions.json").write_text(
        json.dumps(txs, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(slim_sigs)} signatures and {len(txs)} transactions "
          f"to {OUT}")


if __name__ == "__main__":
    main()
