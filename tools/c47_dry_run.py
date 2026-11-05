"""C47 dry run — verification only, no key, no posting, temporary state.

Two runs, both read-only:

  A. day 20758 against the LIVE devnet Worker and the LIVE chain anchors,
     with both flags OFF.  Expect: every payout rule passes (the only
     finding allowed is `no_payout_key`, because no key file is opened).

  B. the GOLDEN day 20764 with LOG_V2_FROM_DAY set to that day only —
     synthetic and entirely offline, driven by the spec bundle's own files.

Nothing is signed, nothing is posted, no key file is read, and the state
directory is a throwaway that the caller deletes.

    python tools/c47_dry_run.py --state /tmp/c47-dryrun-state
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payout import constants as C                                   # noqa: E402
from payout import log_v2 as V2                                     # noqa: E402
from payout.chain_anchors import ChainAnchorBuilder                 # noqa: E402
from payout.golden_v2 import GoldenVectors                          # noqa: E402
from payout.ledger import DecisionLedger                            # noqa: E402
from payout.routes import PayoutRoutes                              # noqa: E402
from payout.service import PayoutSignerService                      # noqa: E402
from payout.sources import AnchorSource, LogSource, Settings, TargetsTable  # noqa: E402

COORDINATOR = "https://life-coordinator-devnet.lifecompute313.workers.dev"
PROGRAM_ID = "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"
LIFE_MINT = "FCwdMRVwxmrnftDgKC2qjkHThxRkYFUUEQieegUdKp3p"
TARGETS = "/root/api/c5b-bundle/targets_table.json"
ANCHORS = "/root/payout-work/anchors.json"
GENESIS_DAY = 20757


def run_a(state_dir: Path, day: int = 20758) -> int:
    print(f"── A. live day {day}, both flags OFF "
          f"(LOG_V2_FROM_DAY={C.LOG_V2_FROM_DAY_DEFAULT}, "
          f"VALIDATOR_PAY_FROM_DAY={C.VALIDATOR_PAY_FROM_DAY_DEFAULT})")
    svc = PayoutSignerService(
        LogSource(COORDINATOR, timeout=20.0, strict_closed=True),
        AnchorSource.from_json_file(ANCHORS),
        Settings(program_id=PROGRAM_ID, life_mint=LIFE_MINT, holdback_days=0),
        TargetsTable.from_file(TARGETS),
        DecisionLedger(state_dir / "a"),
        signer=None,                       # NO key file is ever opened
        log_v2_from_day=C.LOG_V2_FROM_DAY_DEFAULT,
        validator_pay_from_day=C.VALIDATOR_PAY_FROM_DAY_DEFAULT)
    print(f"   log_v2_active({day}) = {svc.log_v2_active(day)}   "
          f"validator_pay_active({day}) = {svc.validator_pay_active(day)}")

    chunks = PayoutRoutes(COORDINATOR, timeout=20.0).chunks_from_envelope(
        PayoutRoutes(COORDINATOR, timeout=20.0).payout_envelope(day))
    print(f"   GET /payout/{day}: {len(chunks)} chunk(s), "
          f"batchId(s) {[c.get('batchId') for c in chunks]}")

    decision = svc.process_day(day, chunks, genesis_day=GENESIS_DAY)
    rules = sorted({f.code for r in decision.results for f in r.findings})
    for r in decision.results:
        print(f"   batchId {r.batch_id} seq {r.seq}: totalRaw={r.total_raw} "
              f"batchHash={r.batch_hash_recomputed}")
    print(f"   findings: {rules or '(none)'}")
    ok = rules in ([], ["no_payout_key"])
    print(f"   => {'ACCEPTED (no key loaded, so unsigned)' if ok else 'REFUSED'}")
    for reason in decision.reasons():
        if not reason.startswith("no_payout_key"):
            print(f"      {reason}")
    return 0 if ok else 1


def run_b(state_dir: Path) -> int:
    g = GoldenVectors()
    day = g.manifest["day_num"]
    print(f"── B. golden day {day} ({g.manifest['day']}), "
          f"LOG_V2_FROM_DAY={day} for THAT DAY ONLY — synthetic, offline")
    ledger = V2.SignedRequestLedger(state_dir / "b")
    lines = [V2.SignedLine.from_json(s) for s in g.signed_raw]
    result = V2.evaluate_day(
        canonical_lines=g.canonical, signed_lines=lines,
        scoring_config=g.scoring_config, registry=g.registry,
        expected_canonical_hash=g.manifest["canonical_hash"],
        expected_signed_hash=g.manifest["signed_hash"],
        anchor_v2_memo=g.manifest["anchor_v2_memo"],
        expectations=g.expectations(),
        modality_of_target=lambda t: "protein",
        ledger=ledger)
    print(f"   scoring_config sha256 = {g.scoring_config.sha256}")
    print(f"   anchor /2 memo        = {g.manifest['anchor_v2_memo'][:60]}…")
    print(f"   verdict               = {result.code}  ({result.detail})")
    for rec in result.records:
        print(f"   record result_id={rec.result_id} "
              f"validator={rec.validator_wallet[:8]}… "
              f"score={rec.validator_score} decision={rec.coord_decision} "
              f"hit={rec.hit_status} miner_req={rec.miner_req[:8]}…")
    if result.accepted:
        V2.commit_day_to_ledger(lines, g.canonical, ledger)
        print(f"   ledger entries written: {len(ledger)}")
    # The full mutation table, for the report.
    cases = g.run_all()
    failed = [c for c in cases if not c.passed]
    print(f"   mutation table: {len(cases) - len(failed)}/{len(cases)} "
          f"as expected")
    return 0 if (result.accepted and not failed) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True,
                    help="throwaway state directory (deleted by the caller)")
    ap.add_argument("--day", type=int, default=20758)
    args = ap.parse_args()
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    rc = run_a(state, args.day)
    print()
    rc |= run_b(state)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
