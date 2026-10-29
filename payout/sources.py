"""Read-only sources of truth for the payout signer.

Three independent inputs, all fail-closed:

  LogSource     the PUBLIC log — GET /log/{day} (hash manifest, API_CONTRACT
                §2.11), the canonical daily NDJSON body, and GET /feed (§2.9).
  AnchorSource  the anchored day->logHash map (on-chain `anchor_hash`,
                BATCH_SPEC §9).  Mocked in tests; no chain is contacted.
  OperatorInputs  facts a batch depends on that the public log does NOT carry
                and BATCH_SPEC.md does not derive: target->modality, flagged
                wallets, holdback forfeits.  Required; a missing file or a
                missing target is a refusal, never a guess.

Any transport failure raises CoordinatorUnreachable, which the service turns
into "no signature".  Nothing here retries forever and nothing falls back to
cached or default data.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .batch_hash import day_to_iso, log_hash_hex
from .errors import CoordinatorUnreachable


@dataclass(frozen=True)
class LogRecord:
    """One line of the canonical daily log (BATCH_SPEC §9).

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


@dataclass
class DayLog:
    day: int
    records: list[LogRecord]
    raw_bytes: bytes
    manifest: dict[str, Any]

    @property
    def computed_log_hash(self) -> str:
        return log_hash_hex(self.raw_bytes)

    @property
    def manifest_results_sha256(self) -> str | None:
        r = self.manifest.get("results")
        return str(r.get("sha256")) if isinstance(r, dict) and r.get("sha256") else None


class LogSource:
    """HTTP reader for the public log.  No auth: all three routes are public."""

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
        """API_CONTRACT §2.11 GET /log/{YYYY-MM-DD} — the hash manifest."""
        obj = self._get_json(f"/log/{day_to_iso(day)}")
        if not isinstance(obj, dict):
            raise CoordinatorUnreachable("GET /log/{day} returned a non-object")
        return obj

    def canonical_log_bytes(self, day: int) -> bytes:
        """The canonical daily log NDJSON body (BATCH_SPEC §9).

        API_CONTRACT.md §2.11 publishes only the log's SHA-256, not its body,
        so the body is read from the archive route the coordinator serves it
        on.  The bytes are returned verbatim — hashing must see exactly what
        the archive stored.
        """
        return self._get(f"/log/{day_to_iso(day)}/canonical")

    def day_log(self, day: int) -> DayLog:
        manifest = self.log_manifest(day)
        raw = self.canonical_log_bytes(day)
        records: list[LogRecord] = []
        for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(LogRecord.from_json(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise CoordinatorUnreachable(
                    f"canonical log day {day} line {lineno} is unparseable: {exc}"
                ) from None
        return DayLog(day=day, records=records, raw_bytes=raw, manifest=manifest)

    def feed(self) -> list[dict[str, Any]]:
        """API_CONTRACT §2.9 GET /feed — wallet-scrubbed recent results.

        The only public carrier of `submitted_at`, which the wallet-age
        holdback (BATCH_SPEC §5.1.3) is measured from.  Joined to the log by
        `molecule_hash`.
        """
        obj = self._get_json("/feed")
        if not isinstance(obj, dict) or not isinstance(obj.get("results"), list):
            raise CoordinatorUnreachable("GET /feed returned an unexpected shape")
        return obj["results"]


class AnchorSource:
    """The anchored day -> logHash map (BATCH_SPEC §9 `anchor_hash`).

    In production this reads the on-chain anchor account.  In tests it is a
    plain JSON mapping; either way a missing day is fatal, never assumed OK.
    """

    def __init__(self, mapping: dict[int, str]):
        self._map = {int(k): str(v).lower() for k, v in mapping.items()}

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


@dataclass
class OperatorInputs:
    """Facts a batch depends on that the public log does not carry.

    BATCH_SPEC.md references `wallet_flags`, `wallet_holdback_forfeit` and a
    modality per target, but defines no public route for any of them and the
    canonical log line (§9) has no modality field.  Rather than guess, the
    signer requires them as an explicit, reviewable operator file and refuses
    whenever a batch depends on a value that file does not contain.
    """
    target_modality: dict[str, str] = field(default_factory=dict)
    flagged_wallets: set[str] = field(default_factory=set)
    forfeited_result_ids: set[int] = field(default_factory=set)
    source_path: str | None = None

    @classmethod
    def from_json_file(cls, path: str | Path) -> "OperatorInputs":
        p = Path(path)
        if not p.is_file():
            raise CoordinatorUnreachable(f"operator inputs file not found: {p}")
        try:
            obj = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise CoordinatorUnreachable(
                f"operator inputs file is not valid JSON: {exc}") from None
        return cls(
            target_modality={str(k): str(v)
                             for k, v in (obj.get("target_modality") or {}).items()},
            flagged_wallets={str(w) for w in (obj.get("flagged_wallets") or [])},
            forfeited_result_ids={int(r) for r in (obj.get("forfeited_result_ids") or [])},
            source_path=str(p),
        )
