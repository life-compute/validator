"""`python -m payout.cli` — the payout checker's command line.

    python -m payout.cli sign --coordinator URL --day N --out FILE [--post]
    python -m payout.cli keygen --out PATH
    python -m payout.cli pubkey --key PATH

What `sign` does, in order, all fail-closed (§C5r.3):

  1. DAY-CLOSE GATE.  `GET /log/{day}` must say `closed: true` EXPLICITLY —
     for the batch day AND for every day of the §6a replay range
     (GENESIS_DAY..day).  A `closed: false` or a manifest that merely omits
     the key is refused as `day_not_closed`.  This is checked FIRST.
  2. FETCH THE BATCH.  `GET /payout/{day}`, take `chunks[]` out of the
     envelope (§C5r.4).
  3. VERIFY.  The whole existing pipeline: canonical-log recomputation, the
     §6a replay baseline, the anchored log hash, and every §5.0 rule in
     order.  The batch hash is RECOMPUTED from the entries; the hash the
     envelope carries is never trusted.
  4. SIGN OR REFUSE.  Only an accepted day produces a file.  A refusal writes
     NO file and prints the FIRST rule that failed.
  5. NEVER TWICE.  A batchId in the existing DecisionLedger is never signed
     again.

Configuration:
  PAYOUT_KEYPAIR   REQUIRED, no default.  Refuses to start when unset, and
                   refuses when its public key equals the validator's
                   verdict-signing key (VALIDATOR_KEYPAIR).
  PROGRAM_ID / LIFE_MINT   required (§16), no defaults.
  PAYOUT_STATE_DIR   ledger location (default ./payout_state).
  PAYOUT_ANCHORS     JSON file of day -> anchored logHash (§9).

`--post` additionally submits the file's EXACT bytes to
`POST /payout/{day}/signature`.  Without it the command only writes the file.

`keygen` creates a NEW payout key (os.urandom(32) seed, Ed25519 public key
derived with the same solders library the signer signs with) in the Solana
keypair format: a JSON array of 64 numbers, the 32-byte seed followed by the
32-byte public key.  It refuses to overwrite an existing path (O_EXCL), makes
the file mode 0600, self-checks the key by signing+verifying a test message
and by reading the file back, and prints ONLY the base58 public key and the
path.  `pubkey` prints the base58 public key of an existing file, nothing
else.  Neither command ever prints, logs or returns the seed.

No key material is ever printed: only public keys, hashes and signatures.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import constants as C
from .batch_hash import batch_hash_bytes, day_to_iso
from .errors import (
    DAY_NOT_CLOSED,
    DayNotClosed,
    PayoutError,
    REPEATED_BATCH_ID,
)
from .keygen import generate_keypair_file, read_pubkey
from .ledger import DecisionLedger
from .recompute import replay_days_required
from .routes import PayoutRoutes
from .service import PayoutSignerService
from .sigfile import build_signature_file, serialize_signature_file
from .signer_key import PayoutSigner
from .sources import AnchorSource, LogSource, Settings, TargetsTable

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_CONFIG = 3


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _build_service(args, env: dict[str, str]) -> tuple[PayoutSignerService,
                                                       PayoutRoutes,
                                                       PayoutSigner,
                                                       LogSource]:
    """Wire the checker up.  Any missing required setting raises."""
    settings = Settings.from_env(env)

    table_path = (env.get("TARGETS_TABLE") or "").strip()
    if not table_path:
        raise PayoutError(
            "TARGETS_TABLE must point at the SHA-verified targets_table.json "
            "(BATCH_SPEC §12); it has no default")
    targets = TargetsTable.from_file(table_path)

    anchors_path = (env.get("PAYOUT_ANCHORS") or "").strip()
    if not anchors_path:
        raise PayoutError(
            "PAYOUT_ANCHORS must point at the anchored day->logHash JSON "
            "(BATCH_SPEC §9); it has no default")
    anchors = AnchorSource.from_json_file(anchors_path)

    # §C5r.3: the payout CLI requires an EXPLICIT closed:true.
    log_source = LogSource(args.coordinator, timeout=args.timeout,
                           strict_closed=True)
    routes = PayoutRoutes(args.coordinator, timeout=args.timeout)

    state_dir = (env.get("PAYOUT_STATE_DIR") or "payout_state").strip()
    ledger = DecisionLedger(state_dir)

    # PAYOUT_KEYPAIR is required and must differ from the verdict key.
    signer = PayoutSigner.from_env(env)

    service = PayoutSignerService(log_source, anchors, settings, targets,
                                  ledger, signer)
    return service, routes, signer, log_source


def _closed_gate(log_source: LogSource, day: int, genesis_day: int) -> None:
    """§C5r.3 step 1: every day of the replay range must be closed:true.

    Runs BEFORE the batch is fetched and before any log body is read, so an
    open day can never reach a payout computation.
    """
    for d in replay_days_required(day, genesis_day):
        manifest = log_source.log_manifest(d)
        closed = manifest.get("closed")
        if closed is not True:
            which = ("the batch day" if d == day
                     else f"replay day {d} (range {genesis_day}..{day})")
            raise DayNotClosed(
                f"GET /log/{day_to_iso(d)} does not state closed=true "
                f"(closed={closed!r}) — {which} is not provably closed; "
                f"§C5r.3 forbids signing, so no signature file is written")


def cmd_sign(args, env: dict[str, str] | None = None) -> int:
    env = dict(os.environ if env is None else env)
    out_path = Path(args.out)

    try:
        service, routes, signer, log_source = _build_service(args, env)
    except PayoutError as exc:
        _eprint(f"REFUSING TO START: {exc}")
        return EXIT_CONFIG

    day = int(args.day)
    genesis_day = int(args.genesis_day)

    # ── 1. day-close gate, batch day AND whole replay range ────────────────
    try:
        _closed_gate(log_source, day, genesis_day)
    except DayNotClosed as exc:
        print(f"REFUSED day {day}: {DAY_NOT_CLOSED}")
        _eprint(str(exc))
        return EXIT_REFUSED
    except PayoutError as exc:
        print(f"REFUSED day {day}: coordinator_unreachable")
        _eprint(str(exc))
        return EXIT_REFUSED

    # ── 2. fetch the batch out of the GET /payout/{day} envelope ───────────
    try:
        envelope = routes.payout_envelope(day)
        chunks = routes.chunks_from_envelope(envelope)
    except PayoutError as exc:
        print(f"REFUSED day {day}: coordinator_unreachable")
        _eprint(str(exc))
        return EXIT_REFUSED

    # ── replay guard: never sign a batchId twice (checked before work) ─────
    already = service.ledger.signed_batch_ids
    for c in chunks:
        try:
            bid = int(c.get("batchId"))
        except (TypeError, ValueError):
            continue
        if bid in already:
            print(f"REFUSED day {day}: {REPEATED_BATCH_ID}")
            _eprint(f"batchId {bid} is already in the ledger at "
                    f"{service.ledger.index_path}; refusing to sign it twice")
            return EXIT_REFUSED

    # ── 3. verify with ALL the existing logic ──────────────────────────────
    decision = service.process_day(day, chunks, genesis_day=genesis_day)

    if not decision.signed:
        rule = decision.primary_rule or "refused"
        print(f"REFUSED day {day}: {rule}")
        for reason in decision.reasons()[:1]:
            _eprint(reason)
        if out_path.exists():
            _eprint(f"NOTE: {out_path} already existed and was NOT modified")
        return EXIT_REFUSED

    # ── 4. accepted: write the §C5r.1 signature file ───────────────────────
    ordered: list[tuple[int, str]] = []
    for c in chunks:
        bid = int(c["batchId"])
        sig = decision.signatures.get(bid)
        if sig is None:
            print(f"REFUSED day {day}: missing_signature")
            _eprint(f"no signature was produced for batchId {bid}")
            return EXIT_REFUSED
        ordered.append((bid, sig))

    obj = build_signature_file(signer.pubkey_hex, ordered)
    body = serialize_signature_file(obj)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(body)

    print(f"ACCEPTED day {day}: wrote {out_path} "
          f"({len(ordered)} chunk(s), signer {signer.pubkey_hex})")

    # ── 5. optional submission of the EXACT file bytes ─────────────────────
    if args.post:
        try:
            resp = routes.post_signature(day, body)
        except PayoutError as exc:
            _eprint(f"POST failed: {exc}")
            return EXIT_REFUSED
        print(f"POSTED day {day}: {json.dumps(resp, sort_keys=True)}")

    return EXIT_OK


def cmd_keygen(args, env: dict[str, str] | None = None) -> int:
    """Create a brand-new payout key.  Prints the PUBLIC key and the path only.

    Refuses to overwrite; file is created O_EXCL at mode 0600; the key is
    self-checked (sign+verify, and read back off disk) before success is
    reported.  The seed is never printed, logged or returned.
    """
    try:
        pubkey = generate_keypair_file(args.out)
    except PayoutError as exc:
        _eprint(f"REFUSING TO CREATE KEY: {exc}")
        return EXIT_CONFIG

    print(f"pubkey: {pubkey}")
    print(f"path: {Path(args.out)}")
    return EXIT_OK


def cmd_pubkey(args, env: dict[str, str] | None = None) -> int:
    """Print the public key of an existing key file.  Nothing else."""
    try:
        print(read_pubkey(args.key))
    except PayoutError as exc:
        _eprint(f"cannot read public key: {exc}")
        return EXIT_CONFIG
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m payout.cli",
        description="Independent payout-batch checker and online signer.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser(
        "sign",
        help="verify a day's payout batch and sign it only if it is accepted")
    s.add_argument("--coordinator", required=True,
                   help="coordinator base URL, e.g. https://api.example.org")
    s.add_argument("--day", required=True, type=int,
                   help="day number (floor(unix_ms / 86_400_000))")
    s.add_argument("--out", required=True,
                   help="signature file to write when the batch is ACCEPTED")
    s.add_argument("--post", action="store_true",
                   help="also POST the file to /payout/{day}/signature")
    s.add_argument("--timeout", type=float, default=15.0,
                   help="HTTP timeout in seconds (default 15)")
    s.add_argument("--genesis-day", type=int, default=C.GENESIS_DAY,
                   dest="genesis_day",
                   help=f"§6a replay start (default {C.GENESIS_DAY})")
    s.set_defaults(func=cmd_sign)

    k = sub.add_parser(
        "keygen",
        help="create a NEW payout key file (never overwrites; prints only the "
             "public key)")
    k.add_argument("--out", required=True,
                   help="path to create, e.g. /root/keys/payout-online.json; "
                        "must not already exist")
    k.set_defaults(func=cmd_keygen)

    g = sub.add_parser(
        "pubkey",
        help="print the base58 public key of an existing key file")
    g.add_argument("--key", required=True, help="path to a Solana keypair file")
    g.set_defaults(func=cmd_pubkey)

    return p


def main(argv: Sequence[str] | None = None,
         env: dict[str, str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    return args.func(args, env)


if __name__ == "__main__":
    raise SystemExit(main())
