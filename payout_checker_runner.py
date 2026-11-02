#!/usr/bin/env python3
"""Launcher for the unattended payout checker — the ONLY thing that reads the clock.

BATCH_SPEC §2 (C5j) forbids any wall-clock call inside the `payout` package,
and tests/test_payout_signer.py::test_the_payout_package_contains_no_wall_clock_call
enforces it over the executable source of every module in that package.  The
scheduler layer still needs an instant — to know how far to scan and to stamp
status.json — so that instant is read HERE, outside the package, and injected
into payout.autorun.main().

It never reaches a payout decision: the checker decides from the batch's day
number and the canonical logs alone.

    python3 payout_checker_runner.py --config /root/payout-work/service.env

Exit codes are payout.autorun's: 0 fine, 2 a refusal or error needs attention,
3 could not start.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from payout.autorun import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(run_ms=int(time.time() * 1000)))
