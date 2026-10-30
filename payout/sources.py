"""Read-only sources of truth for the payout signer.

Inputs, all fail-closed:

  Settings      PROGRAM_ID and LIFE_MINT — REQUIRED, no defaults (§16).
  TargetsTable  targets_table.json, whose SHA-256 MUST equal the value
                published in BATCH_SPEC §12.  Modality and tier come from
                here and from nowhere else.
  LogSource     the PUBLIC log — GET /log/{day} (hash manifest, §2.11) and
                GET /log/{day}/canonical (the exact hashed bytes, §2.16).
  AnchorSource  the anchored day->logHash map (on-chain `anchor_hash`, §9).
                Mocked in tests; no chain is contacted.

C5b2 change: there is no OperatorInputs any more.  Wallet age, flags and
holdback forfeits are derived ONLY from the canonical log's lifecycle events
(§13), and modality/tier ONLY from targets_table.json (§12).  The operator
supplies no payout-affecting facts at all.

Any transport failure raises CoordinatorUnreachable, which the service turns
into "no signature".  Nothing here retries forever and nothing falls back to
cached or default data.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .batch_hash import day_to_iso, log_hash_hex, pubkey_bytes
from .constants import TARGETS_TABLE_SHA256
from .errors import CoordinatorUnreachable, PayoutConfigError

# Lifecycle event names defined by BATCH_SPEC §13.
EV_FIRST_SEEN = "wallet_first_seen"
EV_FLAGGED = "wallet_flagged"
EV_UNFLAGGED = "wallet_unflagged"
EV_FORFEIT = "holdback_forfeit"
KNOWN_EVENTS = frozenset({EV_FIRST_SEEN, EV_FLAGGED, EV_UNFLAGGED, EV_FORFEIT})


# ── required configuration (§16) ─────────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    """PROGRAM_ID and LIFE_MINT are required settings with NO defaults (§16).

    A checker configured with different values cannot verify any fixture
    batch, which is the point: the chain constants are committed to by every
    batch hash (§4), so getting them wrong must fail loudly, not silently.
    """
    program_id: str
    life_mint: str

    def __post_init__(self) -> None:
        for name, val in (("PROGRAM_ID", self.program_id),
                          ("LIFE_MINT", self.life_mint)):
            if not isinstance(val, str) or not val.strip():
                raise PayoutConfigError(
                    f"{name} is a required setting and has no default (§16)")
            try:
                pubkey_bytes(val)
            except ValueError as exc:
                raise PayoutConfigError(
                    f"{name} is not a valid base58 32-byte public key: {exc}"
                ) from None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        import os
        env = dict(os.environ if env is None else env)
        missing = [k for k in ("PROGRAM_ID", "LIFE_MINT")
                   if not (env.get(k) or "").strip()]
        if missing:
            raise PayoutConfigError(
                f"{' and '.join(missing)} must be set; BATCH_SPEC §16 makes "
                f"them required checker configuration with no default")
        return cls(program_id=env["PROGRAM_ID"].strip(),
                   life_mint=env["LIFE_MINT"].strip())


# ── targets table (§12) ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class TargetInfo:
    id: str
    modality: str
    tier: str


class TargetsTable:
    """targets_table.json — the ONLY source of modality and tier (§12).

    The file's SHA-256 is verified against the value published in §12 before
    any entry is read.  A mismatch is fatal: a swapped table could silently
    re-price every payout.
    """

    def __init__(self, entries: list[dict[str, Any]], sha256: str):
        self.sha256 = sha256
        self._by_id: dict[str, TargetInfo] = {}
        for row in entries:
            if not isinstance(row, dict):
                raise PayoutConfigError("targets_table.json rows must be objects")
            tid, modality, tier = row.get("id"), row.get("modality"), row.get("tier")
            if not all(isinstance(x, str) and x for x in (tid, modality, tier)):
                raise PayoutConfigError(
                    f"targets_table.json row missing id/modality/tier: {row!r}")
            tid, modality, tier = str(tid), str(modality), str(tier)
            if tid in self._by_id:
                raise PayoutConfigError(
                    f"targets_table.json contains duplicate target id {tid!r}")
            self._by_id[tid] = TargetInfo(id=tid, modality=modality, tier=tier)

    @classmethod
    def from_file(cls, path: str | Path,
                  expected_sha256: str = TARGETS_TABLE_SHA256) -> "TargetsTable":
        p = Path(path)
        if not p.is_file():
            raise PayoutConfigError(f"targets_table.json not found: {p}")
        raw = p.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha256.lower():
            raise PayoutConfigError(
                f"targets_table.json SHA-256 {actual} does not match the value "
                f"published in BATCH_SPEC §12 ({expected_sha256}); refusing to "
                f"price any payout from an unverified target table")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayoutConfigError(
                f"targets_table.json is not valid JSON: {exc}") from None
        if not isinstance(obj, list):
            raise PayoutConfigError("targets_table.json must be a JSON array")
        return cls(obj, sha256=actual)

    def get(self, target: str) -> TargetInfo | None:
        return self._by_id.get(target)

    def __contains__(self, target: object) -> bool:
        return target in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def by_tier(self, tier: str) -> list[str]:
        return sorted(t.id for t in self._by_id.values() if t.tier == tier)


# ── canonical log ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LogRecord:
    """One verdict line of the canonical daily log (§9).

    Presence of a line in the canonical daily log IS the confirmation: §9
    defines the file as "each line is a confirmed verdict record".
    """
    result_id: int
    wallet: str
    target: str
    molecule_hash: str
    archived_at: int
    coord_score: float

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "LogRecord":
        missing = [k for k in ("result_id", "wallet", "target", "molecule_hash",
                               "archived_at") if k not in obj]
        if missing:
            raise ValueError(f"canonical log line missing {missing}: {obj!r}")
        return cls(
            result_id=int(obj["result_id"]),
            wallet=str(obj["wallet"]),
            target=str(obj["target"]),
            molecule_hash=str(obj["molecule_hash"]),
            archived_at=int(obj["archived_at"]),
            coord_score=float(obj.get("coord_score", 0.0)),
        )


@dataclass(frozen=True)
class LogEvent:
    """One lifecycle event line of the canonical daily log (§13)."""
    event: str
    wallet: str
    time_ms: int
    reason: str | None = None

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "LogEvent":
        for k in ("event", "wallet", "time_ms"):
            if k not in obj:
                raise ValueError(f"canonical log event missing {k!r}: {obj!r}")
        return cls(event=str(obj["event"]), wallet=str(obj["wallet"]),
                   time_ms=int(obj["time_ms"]),
                   reason=str(obj["reason"]) if obj.get("reason") else None)


@dataclass
class WalletFacts:
    """Everything the log says about one wallet (§13) — the ONLY source.

    first_seen_ms  None when the wallet has no wallet_first_seen event, which
                   makes its age unprovable and any payment to it a refusal.
    flagged        latest wallet_flagged with no later wallet_unflagged.
    forfeited      a holdback_forfeit event was emitted; forfeiture is
                   permanent (§5.1.5), so there is no un-forfeit event.
    """
    wallet: str
    first_seen_ms: int | None = None
    flagged: bool = False
    flagged_at_ms: int | None = None
    forfeited: bool = False
    forfeited_at_ms: int | None = None
    forfeit_reason: str | None = None


@dataclass
class DayLog:
    day: int
    records: list[LogRecord]
    events: list[LogEvent]
    raw_bytes: bytes
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def computed_log_hash(self) -> str:
        return log_hash_hex(self.raw_bytes)

    @property
    def manifest_log_hash(self) -> str | None:
        v = self.manifest.get("log_hash")
        return str(v).lower() if isinstance(v, str) else None

    @property
    def manifest_results_sha256(self) -> str | None:
        r = self.manifest.get("results")
        return str(r.get("sha256")).lower() if isinstance(r, dict) and r.get("sha256") else None

    def wallet_facts(self, carry_in: dict[str, WalletFacts] | None = None
                     ) -> dict[str, WalletFacts]:
        """Fold the day's lifecycle events into per-wallet facts (§13).

        `carry_in` lets a multi-day run keep facts emitted on earlier days
        (a wallet_first_seen is emitted once, on the wallet's first day).
        Events are applied in time_ms order so flag/unflag resolves correctly.
        """
        facts: dict[str, WalletFacts] = {
            w: WalletFacts(wallet=f.wallet, first_seen_ms=f.first_seen_ms,
                           flagged=f.flagged, flagged_at_ms=f.flagged_at_ms,
                           forfeited=f.forfeited,
                           forfeited_at_ms=f.forfeited_at_ms,
                           forfeit_reason=f.forfeit_reason)
            for w, f in (carry_in or {}).items()
        }
        for ev in sorted(self.events, key=lambda e: (e.time_ms, e.event)):
            f = facts.setdefault(ev.wallet, WalletFacts(wallet=ev.wallet))
            if ev.event == EV_FIRST_SEEN:
                # Earliest wins: §13.1 emits it once, but be order-safe.
                if f.first_seen_ms is None or ev.time_ms < f.first_seen_ms:
                    f.first_seen_ms = ev.time_ms
            elif ev.event == EV_FLAGGED:
                f.flagged = True
                f.flagged_at_ms = ev.time_ms
            elif ev.event == EV_UNFLAGGED:
                f.flagged = False
            elif ev.event == EV_FORFEIT:
                f.forfeited = True
                f.forfeited_at_ms = ev.time_ms
                f.forfeit_reason = ev.reason
        return facts


class LogSource:
    """HTTP reader for the public log.  No auth: the routes are public."""

    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── transport ───────────────────────────────────────────────────────────
    def _get(self, path: str) -> bytes:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                if resp.status != 200:
                    raise CoordinatorUnreachable(
                        f"GET {path} returned HTTP {resp.status}")
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise CoordinatorUnreachable(f"GET {path} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            # Never include the full URL: it can carry credentials.
            raise CoordinatorUnreachable(
                f"GET {path} failed: {type(exc).__name__}") from None

    def _get_json(self, path: str) -> Any:
        raw = self._get(path)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise CoordinatorUnreachable(f"GET {path} returned non-JSON body") from None

    # ── public log routes ───────────────────────────────────────────────────
    def log_manifest(self, day: int) -> dict[str, Any]:
        """§2.11 GET /log/{YYYY-MM-DD} — the published hash manifest."""
        obj = self._get_json(f"/log/{day_to_iso(day)}")
        if not isinstance(obj, dict):
            raise CoordinatorUnreachable("GET /log/{day} returned a non-object")
        return obj

    def canonical_log_bytes(self, day: int) -> bytes:
        """§2.16 GET /log/{day}/canonical — the exact bytes that are hashed."""
        return self._get(f"/log/{day_to_iso(day)}/canonical")

    def day_log(self, day: int) -> DayLog:
        """Fetch manifest + canonical body and parse both line classes (§13)."""
        manifest = self.log_manifest(day)
        raw = self.canonical_log_bytes(day)
        records: list[LogRecord] = []
        events: list[LogEvent] = []
        for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("line is not a JSON object")
                if "event" in obj:
                    ev = LogEvent.from_json(obj)
                    if ev.event not in KNOWN_EVENTS:
                        raise ValueError(
                            f"unknown lifecycle event {ev.event!r}; BATCH_SPEC "
                            f"§13 defines only {sorted(KNOWN_EVENTS)}")
                    events.append(ev)
                else:
                    records.append(LogRecord.from_json(obj))
            except (json.JSONDecodeError, ValueError) as exc:
                raise CoordinatorUnreachable(
                    f"canonical log day {day} line {lineno} is unparseable: {exc}"
                ) from None
        return DayLog(day=day, records=records, events=events,
                      raw_bytes=raw, manifest=manifest)


class AnchorSource:
    """The anchored day -> logHash map (§9 `anchor_hash`).

    In production this reads the on-chain anchor account.  In tests it is a
    plain mapping; either way a missing day is fatal, never assumed OK.
    """

    def __init__(self, mapping: dict[int, str]):
        self._map = {int(k): str(v).strip().lower() for k, v in mapping.items()}

    @classmethod
    def from_json_file(cls, path: str | Path) -> "AnchorSource":
        p = Path(path)
        if not p.is_file():
            raise CoordinatorUnreachable(f"anchor file not found: {p}")
        try:
            return cls(json.loads(p.read_text()))
        except json.JSONDecodeError as exc:
            raise CoordinatorUnreachable(f"anchor file is not valid JSON: {exc}") from None

    def anchored_hash(self, day: int) -> str:
        try:
            return self._map[int(day)]
        except KeyError:
            raise CoordinatorUnreachable(
                f"no anchored log hash recorded for day {day}") from None
