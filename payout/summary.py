"""Plain-language batch summary for human review before cold-key signing.

Answers the three questions the reviewer actually has:
  * what does this day pay in total, and is it inside the caps?
  * who are the largest recipients?
  * what changed since the previous signed day?

No jargon beyond the field names in the batch file, and every number is
exact integer arithmetic (raw units converted to LIFE without floats).
"""
from __future__ import annotations

from typing import Any, Sequence

from . import constants as C
from .batch_hash import day_to_iso
from .recompute import ComputedEntry
from .verifier import VerifyResult


def _life(raw: int) -> str:
    return f"{C.raw_to_life(raw)} LIFE"


def _pct(part: int, whole: int) -> str:
    return f"{(part * 100 / whole):.1f}%" if whole else "0.0%"


def summarize_day(
    day: int,
    results: Sequence[VerifyResult],
    entries: Sequence[ComputedEntry],
    previous: dict[str, Any] | None = None,
    top_n: int = 5,
) -> str:
    """Render the human review sheet for one calendar day."""
    lines: list[str] = []
    total = sum(e.amount_raw for e in entries)
    fees = sum(e.validator_fee_raw for e in entries)
    signable = all(r.signable for r in results) and bool(results)

    by_wallet: dict[str, list[ComputedEntry]] = {}
    for e in entries:
        by_wallet.setdefault(e.recipient, []).append(e)

    lines.append(f"PAYOUT REVIEW — day {day} ({day_to_iso(day)})")
    lines.append("=" * 62)
    lines.append("")
    lines.append(f"Decision:          {'READY TO SIGN' if signable else 'REFUSED — DO NOT SIGN'}")
    lines.append(f"Chunks:            {len(results)} "
                 f"(batch ids {', '.join(str(r.batch_id) for r in results) or 'none'})")
    n_results = sum(len(e.results) for e in entries)
    lines.append(f"Results paid:      {n_results} to {len(by_wallet)} wallet(s)")
    lines.append(f"Miner total:       {_life(total)}  ({total:,} raw)")
    lines.append(f"Validator fees:    {_life(fees)}  ({fees:,} raw) — paid separately, "
                 f"NOT part of the batch total or its hash")
    lines.append(f"Minted overall:    {_life(total + fees)}")
    lines.append("")
    lines.append("Caps")
    lines.append("-" * 62)
    lines.append(f"  Per-day cap:     {_life(total)} of {_life(C.CAP_PER_DAY)} "
                 f"used ({_pct(total, C.CAP_PER_DAY)})")
    worst = max((r.total_raw for r in results), default=0)
    lines.append(f"  Largest chunk:   {_life(worst)} of {_life(C.CAP_PER_BATCH)} "
                 f"per-batch cap ({_pct(worst, C.CAP_PER_BATCH)})")
    lines.append(f"  Entries/chunk:   max {C.MAX_BATCH_ENTRIES}; "
                 f"actual {[len(r.expected_entries) for r in results]}")
    per_wallet_counts = {w: sum(len(e.results) for e in v) for w, v in by_wallet.items()}
    busiest = max(per_wallet_counts.values(), default=0)
    lines.append(f"  Per-miner/day:   max {C.DAILY_MINER_CAP} results; "
                 f"busiest wallet has {busiest}")
    lines.append("")

    lines.append(f"Largest recipients (top {top_n})")
    lines.append("-" * 62)
    ranked = sorted(by_wallet.items(),
                    key=lambda kv: (-sum(e.amount_raw for e in kv[1]), kv[0]))
    if not ranked:
        lines.append("  (nothing payable)")
    for wallet, es in ranked[:top_n]:
        amt = sum(e.amount_raw for e in es)
        n_res = sum(len(e.results) for e in es)
        targets = ", ".join(sorted({r.target for e in es for r in e.results}))
        lines.append(f"  {wallet}")
        lines.append(f"      {_life(amt)} over {n_res} result(s) "
                     f"[{_pct(amt, total)} of the day]  targets: {targets}")
    lines.append("")

    lines.append("Change from the previous signed day")
    lines.append("-" * 62)
    if previous is None:
        lines.append("  No previously signed day on record — this is the first "
                     "day this service has reviewed.")
    else:
        p_day = int(previous.get("day", -1))
        p_total = int(previous.get("totalRaw", 0))
        p_entries = int(previous.get("entryCount", 0))
        p_wallets = int(previous.get("walletCount", 0) or 0)
        delta = total - p_total
        sign = "+" if delta >= 0 else "-"
        lines.append(f"  Previous signed day: {p_day} ({day_to_iso(p_day)})")
        lines.append(f"  Total:     {_life(p_total)} -> {_life(total)}  "
                     f"({sign}{_life(abs(delta))})")
        if p_total:
            lines.append(f"             {sign}{abs(delta) * 100 / p_total:.1f}% versus that day")
        lines.append(f"  Entries:   {p_entries} -> {len(entries)}")  # wallet entries
        if p_wallets:
            lines.append(f"  Wallets:   {p_wallets} -> {len(by_wallet)}")
        prev_w = previous.get("recipients") or []
        if prev_w:
            now_w = set(by_wallet)
            new = sorted(now_w - set(prev_w))
            gone = sorted(set(prev_w) - now_w)
            lines.append(f"  New recipients:     {len(new)}"
                         + (f" ({', '.join(w[:8] + '…' for w in new[:5])})" if new else ""))
            lines.append(f"  Dropped recipients: {len(gone)}"
                         + (f" ({', '.join(w[:8] + '…' for w in gone[:5])})" if gone else ""))
    lines.append("")

    all_findings = [f for r in results for f in r.findings]
    if all_findings:
        lines.append("WHY THIS IS REFUSED")
        lines.append("-" * 62)
        for f in all_findings:
            rid = f" [result {f.result_id}]" if f.result_id is not None else ""
            lines.append(f"  * {f.code}{rid}")
            lines.append(f"      {f.message}")
        lines.append("")

    all_warnings = [w for r in results for w in r.warnings]
    seen: set[str] = set()
    uniq = [w for w in all_warnings if not (w.message in seen or seen.add(w.message))]
    if uniq:
        lines.append("Caveats (recorded; they do not permit or block a signature on their own)")
        lines.append("-" * 62)
        for w in uniq:
            lines.append(f"  * {w.code}: {w.message}")
        lines.append("")

    lines.append("What you are signing")
    lines.append("-" * 62)
    for r in results:
        lines.append(f"  batchId {r.batch_id} (day {r.day}, chunk {r.seq}) "
                     f"total {_life(r.total_raw)}")
        lines.append(f"      batch hash: {r.batch_hash_recomputed}")
    lines.append("  The signature covers the C4c batch hash (§4), which commits to")
    lines.append("  the domain tag, program id, LIFE mint, batch id, day, entry count")
    lines.append("  and every (recipient, amount) pair in sorted order — so the")
    lines.append("  recipient list IS inside what you sign.")
    return "\n".join(lines)


def summary_record(day: int, entries: Sequence[ComputedEntry],
                   results: Sequence[VerifyResult]) -> dict[str, Any]:
    """Machine-readable twin of the summary, stored in the ledger for diffing."""
    by_wallet: dict[str, int] = {}
    for e in entries:
        by_wallet[e.recipient] = by_wallet.get(e.recipient, 0) + e.amount_raw
    return {
        "day": day,
        "resultCount": sum(len(e.results) for e in entries),
        "totalRaw": sum(e.amount_raw for e in entries),
        "validatorFeeRaw": sum(e.validator_fee_raw for e in entries),
        "entryCount": len(entries),
        "walletCount": len(by_wallet),
        "recipients": sorted(by_wallet),
        "batchIds": [r.batch_id for r in results],
    }
