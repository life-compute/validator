"""ChainAnchorBuilder — day -> anchored logHash read from the CHAIN (§9).

This replaces the Worker-sourced anchor map.  The anchor the checker verifies
a day against is an SPL Memo transaction the operator published on the
cluster; the coordinator that proposes a batch cannot forge it, so the anchor
is independent of the party being checked.

An anchor transaction is accepted only when ALL of these hold:

  * the transaction SUCCEEDED (`meta.err` is null);
  * it carries an instruction of the Memo program (ANCHOR_MEMO_PROGRAM);
  * the memo text parses EXACTLY as

        LIFE-ANCHOR/1 day=<n> date=<yyyy-mm-dd> log=<64 hex> program=<id>

    or, for a day at or above LOG_V2_FROM_DAY (BATCH_SPEC §17.4),

        LIFE-ANCHOR/2 day=<n> date=<yyyy-mm-dd> log=<64 hex> signed=<64 hex> program=<id>

  * `program=` equals the configured PROGRAM_ID;
  * `date` is the calendar date of `day` (day_to_iso(day) == date);
  * ANCHOR_OPERATOR_PUBKEY is a SIGNER of the transaction.

Anything else (another signer, a malformed memo, a failed transaction, the
wrong program id) is IGNORED and counted — never a refusal on its own, and
never silently treated as an anchor.

ANCHOR VERSION PER DAY (§17.4).  `LOG_V2_FROM_DAY` selects which version a
day REQUIRES: strictly below it a `/1` memo is required exactly as before,
and at or above it a `/2` memo is required and a `/1` for that day is no
longer sufficient (it commits to no `signed=` hash).  The accept rule, the
ignore reasons and the transient/refusal outcomes are IDENTICAL for both
versions — only the memo grammar and the required version differ.

Outcomes this module reports (the caller maps them to run outcomes):

  anchor_missing   TRANSIENT.  No anchor on chain for the day yet.  The day is
                   retried on the next run; it is never recorded as a refusal
                   and never blocks another day.
  anchor_conflict  REFUSAL.  Two valid anchors for the same day disagree on
                   the log hash.
  (anchor_mismatch is decided by the CHECKER, not here: the checker computes
   the log hash from the canonical log it downloaded and compares it with the
   hash this module read from the chain.  The Worker's published value is
   never the authority.)

An RPC failure (transport error, HTTP 429/5xx, JSON-RPC error) raises
AnchorRpcUnavailable, which the caller treats as TRANSIENT for every day:
"the chain could not be read" is never a reason to refuse a batch.

NO WALL CLOCK.  Like every module in this package, this one reads no clock
(BATCH_SPEC §2 / C5j): `finalized` commitment, not a timestamp, is what makes
a cached transaction immutable.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .batch_hash import b58decode, day_to_iso
from .errors import PayoutError

# ── configuration defaults ──────────────────────────────────────────────────
DEFAULT_ANCHOR_RPC_URL = "https://api.devnet.solana.com"
DEFAULT_MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

ANCHOR_MEMO_VERSION = "LIFE-ANCHOR/1"
ANCHOR_MEMO_VERSION_V2 = "LIFE-ANCHOR/2"

# The day from which §17 log format v2 (and therefore the /2 anchor) applies.
# 99999 = OFF, which is the devnet deployment's setting.  Never read from a
# clock: it is a day NUMBER supplied by configuration.
LOG_V2_OFF = 99_999

# Exact shape, anchored at both ends: anything else is "malformed".
_MEMO_RE = re.compile(
    r"^LIFE-ANCHOR/1 day=(?P<day>\d{1,9}) date=(?P<date>\d{4}-\d{2}-\d{2}) "
    r"log=(?P<log>[0-9a-fA-F]{64}) "
    r"program=(?P<program>[1-9A-HJ-NP-Za-km-z]{32,44})$")

# §17.4: same shape plus `signed=`, in that exact position.
_MEMO_V2_RE = re.compile(
    r"^LIFE-ANCHOR/2 day=(?P<day>\d{1,9}) date=(?P<date>\d{4}-\d{2}-\d{2}) "
    r"log=(?P<log>[0-9a-fA-F]{64}) signed=(?P<signed>[0-9a-fA-F]{64}) "
    r"program=(?P<program>[1-9A-HJ-NP-Za-km-z]{32,44})$")

# ── outcome names (shared with payout.autorun) ──────────────────────────────
ANCHOR_MISSING = "anchor_missing"
ANCHOR_MISMATCH = "anchor_mismatch"
ANCHOR_CONFLICT = "anchor_conflict"

# ── why a transaction was ignored (counted in status.json) ──────────────────
IGNORED_FAILED_TX = "failed_transaction"
IGNORED_NO_MEMO = "no_memo_instruction"
IGNORED_MALFORMED = "malformed_memo"
IGNORED_WRONG_PROGRAM = "wrong_program_id"
IGNORED_NOT_OPERATOR = "operator_not_a_signer"
IGNORED_DAY_DATE = "day_date_disagree"
# §17.4: the memo parsed, but its version is not the one this day requires.
IGNORED_WRONG_VERSION = "wrong_anchor_version"


class AnchorRpcUnavailable(PayoutError):
    """The RPC endpoint could not be read.  TRANSIENT — never a refusal."""


class AnchorConflict(PayoutError):
    """Two valid anchors for one day carry different log hashes."""


@dataclass(frozen=True)
class AnchorRecord:
    """One accepted on-chain anchor."""
    day: int
    date: str
    log_hash: str
    program_id: str
    signature: str
    slot: int
    # §17.4: present only on a /2 anchor.  None on /1, where the signed log
    # is not committed to at all.
    signed_hash: str | None = None
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        out = {"day": self.day, "date": self.date, "logHash": self.log_hash,
               "programId": self.program_id, "signature": self.signature,
               "slot": self.slot, "version": self.version}
        if self.signed_hash is not None:
            out["signedHash"] = self.signed_hash
        return out


@dataclass
class ChainScan:
    """What one scan of the operator's history found."""
    anchors: dict[int, AnchorRecord] = field(default_factory=dict)
    conflicts: dict[int, list[AnchorRecord]] = field(default_factory=dict)
    ignored: dict[str, int] = field(default_factory=dict)
    signatures_seen: int = 0
    transactions_fetched: int = 0
    cache_hits: int = 0
    pages: int = 0

    def ignore(self, why: str) -> None:
        self.ignored[why] = self.ignored.get(why, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchors": {str(d): a.as_dict() for d, a in sorted(self.anchors.items())},
            "conflicts": {str(d): [a.as_dict() for a in v]
                          for d, v in sorted(self.conflicts.items())},
            "ignored": dict(sorted(self.ignored.items())),
            "signaturesSeen": self.signatures_seen,
            "transactionsFetched": self.transactions_fetched,
            "cacheHits": self.cache_hits,
            "pages": self.pages,
        }


# ── RPC ─────────────────────────────────────────────────────────────────────
class SolanaRpc:
    """Minimal read-only JSON-RPC client.  POSTs nothing but queries.

    Every failure mode collapses into AnchorRpcUnavailable, which the caller
    treats as transient: an unreadable chain must never turn into a refusal.
    """

    def __init__(self, url: str = DEFAULT_ANCHOR_RPC_URL, timeout: float = 20.0,
                 headers: dict[str, str] | None = None):
        self.url = url
        self.timeout = timeout
        self._headers = dict(headers or {})
        self._headers.setdefault("Content-Type", "application/json")
        self._id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body,
                                     headers=self._headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 429 (rate limit) and every 5xx are transient by construction.
            raise AnchorRpcUnavailable(
                f"{method}: RPC answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            # Never echo the URL: it can carry a token.
            raise AnchorRpcUnavailable(
                f"{method}: {type(exc).__name__}") from None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise AnchorRpcUnavailable(f"{method}: RPC answer is not JSON") from None
        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise AnchorRpcUnavailable(f"{method}: RPC error {code}: {msg}")
        if not isinstance(obj, dict) or "result" not in obj:
            raise AnchorRpcUnavailable(f"{method}: RPC answer carries no result")
        return obj["result"]


# ── the incremental cache ───────────────────────────────────────────────────
class SignatureCache:
    """Signature -> parsed outcome, for FINALIZED transactions only.

    A finalized transaction can never change, so it is fetched exactly once:
    on every later run the cached verdict (anchor record or ignore reason) is
    reused and `getTransaction` is not called again.
    """

    VERSION = 1

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            obj = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(obj, dict) and obj.get("version") == self.VERSION:
            entries = obj.get("signatures")
            if isinstance(entries, dict):
                self._data = entries

    def get(self, signature: str) -> dict[str, Any] | None:
        entry = self._data.get(signature)
        return entry if isinstance(entry, dict) else None

    def put(self, signature: str, entry: dict[str, Any]) -> None:
        self._data[signature] = entry
        self._dirty = True

    def save(self) -> None:
        if self.path is None or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"version": self.VERSION, "signatures": self._data},
            indent=1, sort_keys=True))
        tmp.replace(self.path)
        self._dirty = False

    def __len__(self) -> int:
        return len(self._data)


# ── memo parsing ────────────────────────────────────────────────────────────
def parse_anchor_memo(text: str) -> dict[str, Any] | None:
    """Parse an anchor memo.  Returns None for anything not EXACTLY §1 shape."""
    if not isinstance(text, str):
        return None
    m = _MEMO_RE.match(text)
    if not m:
        return None
    return {"day": int(m.group("day")), "date": m.group("date"),
            "log_hash": m.group("log").lower(), "signed_hash": None,
            "program_id": m.group("program"), "version": 1}


def parse_anchor_memo_v2(text: str) -> dict[str, Any] | None:
    """Parse a LIFE-ANCHOR/2 memo.  None for anything not EXACTLY §17.4."""
    if not isinstance(text, str):
        return None
    m = _MEMO_V2_RE.match(text)
    if not m:
        return None
    return {"day": int(m.group("day")), "date": m.group("date"),
            "log_hash": m.group("log").lower(),
            "signed_hash": m.group("signed").lower(),
            "program_id": m.group("program"), "version": 2}


def parse_any_anchor_memo(text: str) -> dict[str, Any] | None:
    """Parse either version.  The version is reported in `version`."""
    return parse_anchor_memo(text) or parse_anchor_memo_v2(text)


def required_anchor_version(day: int, log_v2_from_day: int = LOG_V2_OFF) -> int:
    """§17.4: which anchor version day `day` must carry.

    Strictly below LOG_V2_FROM_DAY → 1 (unchanged).  At or above it → 2.
    With the flag at its default 99999 every real day requires /1, which is
    exactly the pre-C47 behaviour.
    """
    return 2 if int(day) >= int(log_v2_from_day) else 1


def _memo_texts(tx: dict[str, Any], memo_program: str) -> list[str]:
    """Every memo-program instruction's text (top level and inner)."""
    out: list[str] = []
    message = (((tx.get("transaction") or {}).get("message")) or {})
    groups: list[Any] = [message.get("instructions") or []]
    for inner in ((tx.get("meta") or {}).get("innerInstructions") or []):
        if isinstance(inner, dict):
            groups.append(inner.get("instructions") or [])
    for group in groups:
        if not isinstance(group, list):
            continue
        for ix in group:
            if not isinstance(ix, dict) or ix.get("programId") != memo_program:
                continue
            parsed = ix.get("parsed")
            if isinstance(parsed, str):
                out.append(parsed)
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("memo"), str):
                out.append(parsed["memo"])
                continue
            data = ix.get("data")
            if isinstance(data, str) and data:
                try:
                    out.append(b58decode(data).decode("utf-8"))
                except Exception:
                    out.append("")
    return out


def _signers(tx: dict[str, Any]) -> set[str]:
    message = (((tx.get("transaction") or {}).get("message")) or {})
    keys = message.get("accountKeys") or []
    out: set[str] = set()
    for k in keys:
        if isinstance(k, dict) and k.get("signer") and isinstance(k.get("pubkey"), str):
            out.add(k["pubkey"])
    return out


# ── the builder ─────────────────────────────────────────────────────────────
class ChainAnchorBuilder:
    """Reads the day -> anchored logHash map from the chain.

    `collect()` returns the whole scan (anchors, conflicts, ignore counts);
    `build()` keeps the AnchorBuilder contract used by payout.autorun — a
    plain day -> hash mapping in which a day with no anchor is simply absent
    (the caller turns that into the transient `anchor_missing` outcome).
    """

    name = "chain"

    def __init__(self, program_id: str, operator_pubkey: str, *,
                 rpc_url: str = DEFAULT_ANCHOR_RPC_URL,
                 memo_program: str = DEFAULT_MEMO_PROGRAM,
                 cache_path: str | Path | None = None,
                 rpc: SolanaRpc | None = None,
                 timeout: float = 20.0,
                 headers: dict[str, str] | None = None,
                 page_size: int = 1000,
                 max_pages: int = 50,
                 warn=print,
                 log_v2_from_day: int = LOG_V2_OFF):
        if not (program_id or "").strip():
            raise PayoutError("PROGRAM_ID is required to read on-chain anchors")
        if not (operator_pubkey or "").strip():
            raise PayoutError(
                "ANCHOR_OPERATOR_PUBKEY is required: without the operator's "
                "public key the checker cannot tell whose memo is an anchor")
        self.program_id = program_id.strip()
        self.operator_pubkey = operator_pubkey.strip()
        self.memo_program = (memo_program or DEFAULT_MEMO_PROGRAM).strip()
        self.rpc_url = rpc_url
        self.rpc = rpc or SolanaRpc(rpc_url, timeout=timeout, headers=headers)
        self.cache = SignatureCache(cache_path)
        self.page_size = int(page_size)
        self.max_pages = int(max_pages)
        self.warn = warn
        # §17.4 — which anchor VERSION each day requires.  Default OFF.
        self.log_v2_from_day = int(log_v2_from_day)
        self.last_scan: ChainScan | None = None

    # ── scanning ────────────────────────────────────────────────────────────
    def _signature_pages(self, scan: ChainScan) -> Iterable[list[dict[str, Any]]]:
        before: str | None = None
        for _ in range(self.max_pages):
            opts: dict[str, Any] = {"limit": self.page_size,
                                    "commitment": "finalized"}
            if before is not None:
                opts["before"] = before
            page = self.rpc.call("getSignaturesForAddress",
                                 [self.operator_pubkey, opts])
            if not isinstance(page, list) or not page:
                return
            scan.pages += 1
            yield [p for p in page if isinstance(p, dict)]
            if len(page) < self.page_size:
                return
            last = page[-1]
            before = last.get("signature") if isinstance(last, dict) else None
            if not before:
                return

    def _record_from_tx(self, signature: str, tx: dict[str, Any]
                        ) -> tuple[AnchorRecord | None, str | None]:
        """(record, ignore_reason) for one fetched transaction."""
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None, IGNORED_FAILED_TX
        texts = _memo_texts(tx, self.memo_program)
        if not texts:
            return None, IGNORED_NO_MEMO
        slot = int(tx.get("slot") or 0)
        reason = IGNORED_MALFORMED
        for text in texts:
            # Both grammars are parsed; the per-day version requirement is
            # applied below, so a /2 memo on a flag-off day is reported as
            # the wrong VERSION rather than as a malformed memo.
            parsed = parse_any_anchor_memo(text)
            if parsed is None:
                reason = IGNORED_MALFORMED
                continue
            if parsed["program_id"] != self.program_id:
                reason = IGNORED_WRONG_PROGRAM
                continue
            if day_to_iso(parsed["day"]) != parsed["date"]:
                reason = IGNORED_DAY_DATE
                continue
            want = required_anchor_version(parsed["day"], self.log_v2_from_day)
            if parsed["version"] != want:
                reason = IGNORED_WRONG_VERSION
                continue
            if self.operator_pubkey not in _signers(tx):
                reason = IGNORED_NOT_OPERATOR
                continue
            return AnchorRecord(day=parsed["day"], date=parsed["date"],
                                log_hash=parsed["log_hash"],
                                program_id=parsed["program_id"],
                                signature=signature, slot=slot,
                                signed_hash=parsed["signed_hash"],
                                version=parsed["version"]), None
        return None, reason

    def collect(self, days: Iterable[int] | None = None) -> ChainScan:
        """Scan the operator's history and return every anchor it published."""
        wanted = set(int(d) for d in days) if days is not None else None
        scan = ChainScan()
        for page in self._signature_pages(scan):
            for item in page:
                signature = item.get("signature")
                if not isinstance(signature, str) or not signature:
                    continue
                scan.signatures_seen += 1
                cached = self.cache.get(signature)
                if cached is not None:
                    scan.cache_hits += 1
                else:
                    tx = self.rpc.call("getTransaction", [
                        signature, {"encoding": "jsonParsed",
                                    "commitment": "finalized",
                                    "maxSupportedTransactionVersion": 0}])
                    scan.transactions_fetched += 1
                    if not isinstance(tx, dict):
                        # Not finalized yet (null result): do NOT cache it.
                        continue
                    record, why = self._record_from_tx(signature, tx)
                    cached = (record.as_dict() if record is not None
                              else {"ignored": why})
                    self.cache.put(signature, cached)
                if "ignored" in cached:
                    scan.ignore(str(cached["ignored"]))
                    continue
                record = AnchorRecord(day=int(cached["day"]),
                                      date=str(cached["date"]),
                                      log_hash=str(cached["logHash"]),
                                      program_id=str(cached["programId"]),
                                      signature=str(cached["signature"]),
                                      slot=int(cached["slot"]),
                                      signed_hash=(str(cached["signedHash"])
                                                   if cached.get("signedHash")
                                                   else None),
                                      version=int(cached.get("version") or 1))
                # A cached record was parsed under whatever flag was in force
                # when it was written; re-apply the CURRENT requirement so a
                # flag change cannot be masked by the cache.
                if record.version != required_anchor_version(
                        record.day, self.log_v2_from_day):
                    scan.ignore(IGNORED_WRONG_VERSION)
                    continue
                if wanted is not None and record.day not in wanted:
                    continue
                self._merge(scan, record)
        self.cache.save()
        self.last_scan = scan
        return scan

    @staticmethod
    def _merge(scan: ChainScan, record: AnchorRecord) -> None:
        seen = scan.anchors.get(record.day)
        if seen is None:
            scan.anchors[record.day] = record
            return
        if seen.log_hash == record.log_hash:
            return                               # the same anchor, re-published
        bucket = scan.conflicts.setdefault(record.day, [seen])
        if all(r.signature != record.signature for r in bucket):
            bucket.append(record)

    # ── AnchorBuilder contract ──────────────────────────────────────────────
    def build(self, days: Iterable[int]) -> dict[int, str]:
        wanted = [int(d) for d in days]
        scan = self.collect(wanted)
        if scan.conflicts:
            days_in_conflict = sorted(scan.conflicts)
            raise AnchorConflict(
                f"two valid on-chain anchors disagree for day(s) "
                f"{days_in_conflict}: " + "; ".join(
                    f"day {d} " + " vs ".join(
                        f"{r.log_hash[:8]}…@{r.signature[:8]}…"
                        for r in scan.conflicts[d])
                    for d in days_in_conflict))
        return {d: a.log_hash for d, a in scan.anchors.items() if d in set(wanted)}
