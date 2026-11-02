"""`python -m payout.autorun` — unattended driver for the payout checker.

This module is the SCHEDULER LAYER ONLY.  It decides *which* days to offer to
the checker and what to do with the checker's answer; it contains no payout
rule, recomputes nothing, and signs nothing itself.  Every accept/refuse
decision is made by the existing `payout.cli sign` pipeline
(`PayoutSignerService` in payout/service.py), unchanged.

What one run does, in order, per day:

  1. DISCOVER.  The Worker publishes no "list the closed days" route (GET /log,
     /logs, /days and /payout are all 404 on the devnet deployment), so the
     days are discovered by walking `GET /log/{YYYY-MM-DD}` from the first day
     with data (PAYOUT_FIRST_DAY, default PAYOUT_GENESIS_DAY) to PAYOUT_LAST_DAY
     (default: the day number of the current clock).  A day that is not
     archived (404) or that does not say `closed: true` is skipped, never
     signed.
  2. RETRY A SAVED POST.  If a previous run wrote a signature file but its
     POST failed, THIS run only re-posts the saved bytes.  It never signs
     again: the signature file on disk is the one and only artefact.
  3. SKIP WHAT IS DONE.  A day whose batchId is already in the checker's
     ledger (PAYOUT_STATE_DIR/payout_index.json) is reported `already_signed`
     and is not offered to the signer again.
  4. SKIP A STANDING REFUSAL.  A day the checker refused is reported from the
     recorded refusal (rule + reason) and is NOT re-verified until the batch
     fingerprint changes.
  5. NOTHING TO SIGN.  No batch (404) or a batch whose chunks carry zero
     entries needs no signature and is recorded as such.
  6. SIGN, THEN POST.  Otherwise `payout.cli sign` runs (WITHOUT --post), and
     only if it accepted the day are the file's exact bytes posted to
     `POST /payout/{YYYY-MM-DD}/signature` (the route takes the DATE, not the
     day number).  A failed post leaves the file in place for step 2.

Every run writes PAYOUT_WORK_DIR/status.json and appends to
PAYOUT_WORK_DIR/payout-checker.log.  The exit code is non-zero when a refusal
or an error needs attention.

KEYS.  This module never opens, reads, prints or logs a key file.  It passes
the PAYOUT_KEYPAIR and VALIDATOR_KEYPAIR *paths* through the environment to
the checker, which loads them itself; `_SECRET_ENV_KEYS` are redacted to the
path only in every status record and log line.

ANCHORS (§9).  The checker requires PAYOUT_ANCHORS: day -> anchored logHash.
With ANCHOR_SOURCE=worker this file is built from the Worker's OWN closed-day
manifests, which is NOT an independent anchor — the same party that proposes
the batch supplies the hash it is checked against.  It is therefore allowed
ONLY for a program id in the hard-coded devnet allow-list
(payout.constants.DEVNET_PROGRAM_IDS) and prints a loud warning every run.
`ChainAnchorBuilder` below is the place where on-chain anchors go later.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import cli as payout_cli
from . import constants as C
from .batch_hash import day_to_iso
from .errors import PayoutError
from .ledger import DecisionLedger
from .routes import PayoutRoutes

_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
from http_ua import ua_headers  # noqa: E402

EXIT_OK = 0
EXIT_ATTENTION = 2      # a refusal or a per-day error needs a human
EXIT_CONFIG = 3         # could not even start

# Settings whose VALUE must never be echoed.  (These hold paths, not key
# material, but the redaction keeps a future secret-bearing setting safe.)
_SECRET_ENV_KEYS = ("PAYOUT_KEYPAIR", "VALIDATOR_KEYPAIR")

ANCHOR_WARNING = (
    "*** DEVNET ANCHORS FROM THE WORKER, NOT INDEPENDENT: PAYOUT_ANCHORS is "
    "built from the coordinator's own GET /log/{date} manifests, so the party "
    "that proposes the batch also supplies the log hash it is checked "
    "against. This is accepted ONLY for a hard-coded devnet program id and "
    "MUST be replaced by on-chain anchors before any real cluster. ***")

_REFUSED_RE = re.compile(r"^REFUSED day (\d+): (\S+)", re.M)


def _iso_utc(ms: int) -> str:
    """Format an instant that was GIVEN to us.  This reads no clock.

    Only the launcher outside the package supplies the instant, so the
    package-wide no-wall-clock guard stays satisfied.
    """
    secs = int(ms) // 1000
    days, rem = divmod(secs, 86_400)
    hh, rem = divmod(rem, 3600)
    mm, ss = divmod(rem, 60)
    y, m, d = _civil_from_days(days)
    return f"{y:04d}-{m:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}Z"


def _civil_from_days(z: int) -> tuple[int, int, int]:
    """days-since-epoch -> (year, month, day); Howard Hinnant's civil_from_days."""
    z += 719_468
    era = (z if z >= 0 else z - 146_096) // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return (y + (1 if m <= 2 else 0), m, d)


class ServiceConfigError(PayoutError):
    """The service cannot start with the configuration it was given."""


class AnchorSourceRefused(PayoutError):
    """The configured anchor source is not allowed for this program id."""


# ── configuration ───────────────────────────────────────────────────────────
def load_env_file(path: str | Path, *, require_owner_only: bool = True
                  ) -> dict[str, str]:
    """Read a `KEY=VALUE` configuration file.  No shell, no interpolation.

    The file must be mode 0600 (owner only): it names the key paths the
    unattended service will sign with, so a group/world-readable copy is
    refused rather than used.
    """
    p = Path(path)
    if not p.is_file():
        raise ServiceConfigError(f"configuration file not found: {p}")
    if require_owner_only:
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            raise ServiceConfigError(
                f"{p} is mode {mode:04o}; it must be 0600 (owner only) "
                f"because it names the keys this service signs with")
    out: dict[str, str] = {}
    for lineno, raw in enumerate(p.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise ServiceConfigError(
                f"{p} line {lineno} is not KEY=VALUE")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class ServiceConfig:
    coordinator: str
    program_id: str
    life_mint: str
    targets_table: str
    payout_keypair: str
    validator_keypair: str
    anchors_path: str
    state_dir: str
    work_dir: str
    genesis_day: int
    first_day: int
    last_day: int | None = None
    anchor_source: str = "worker"
    holdback_override: str | None = None
    timeout: float = 20.0

    @property
    def signatures_dir(self) -> Path:
        return Path(self.work_dir) / "signatures"

    @property
    def status_path(self) -> Path:
        return Path(self.work_dir) / "status.json"

    @property
    def log_path(self) -> Path:
        return Path(self.work_dir) / "payout-checker.log"

    @property
    def service_state_path(self) -> Path:
        return Path(self.work_dir) / "service_state.json"

    @classmethod
    def from_mapping(cls, env: dict[str, str]) -> "ServiceConfig":
        def need(key: str) -> str:
            val = (env.get(key) or "").strip()
            if not val:
                raise ServiceConfigError(
                    f"{key} is required in the service configuration and has "
                    f"no default")
            return val

        work_dir = (env.get("PAYOUT_WORK_DIR") or "/root/payout-work").strip()
        genesis_raw = (env.get("PAYOUT_GENESIS_DAY") or "").strip()
        genesis_day = int(genesis_raw) if genesis_raw else C.GENESIS_DAY
        first_raw = (env.get("PAYOUT_FIRST_DAY") or "").strip()
        last_raw = (env.get("PAYOUT_LAST_DAY") or "").strip()
        anchor_source = (env.get("ANCHOR_SOURCE") or "worker").strip().lower()
        if anchor_source not in ("worker", "file", "chain"):
            raise ServiceConfigError(
                f"ANCHOR_SOURCE={anchor_source!r} is not one of "
                f"worker|file|chain")
        for key in ("PAYOUT_KEYPAIR", "VALIDATOR_KEYPAIR", "TARGETS_TABLE"):
            path = (env.get(key) or "").strip()
            if path and not Path(path).is_file():
                raise ServiceConfigError(f"{key} does not name a file")
        return cls(
            coordinator=need("COORDINATOR_URL").rstrip("/"),
            program_id=need("PROGRAM_ID"),
            life_mint=need("LIFE_MINT"),
            targets_table=need("TARGETS_TABLE"),
            payout_keypair=need("PAYOUT_KEYPAIR"),
            validator_keypair=need("VALIDATOR_KEYPAIR"),
            anchors_path=(env.get("PAYOUT_ANCHORS")
                          or str(Path(work_dir) / "anchors.json")).strip(),
            state_dir=(env.get("PAYOUT_STATE_DIR")
                       or "payout_state").strip(),
            work_dir=work_dir,
            genesis_day=genesis_day,
            first_day=int(first_raw) if first_raw else genesis_day,
            last_day=int(last_raw) if last_raw else None,
            anchor_source=anchor_source,
            holdback_override=(env.get("HOLDBACK_DAYS_OVERRIDE") or None),
            timeout=float((env.get("HTTP_TIMEOUT") or "20").strip()),
        )

    def checker_env(self) -> dict[str, str]:
        """The environment the checker CLI is given.  Paths only, no secrets."""
        env = {
            "PROGRAM_ID": self.program_id,
            "LIFE_MINT": self.life_mint,
            "TARGETS_TABLE": self.targets_table,
            "PAYOUT_KEYPAIR": self.payout_keypair,
            "VALIDATOR_KEYPAIR": self.validator_keypair,
            "PAYOUT_ANCHORS": self.anchors_path,
            "PAYOUT_STATE_DIR": self.state_dir,
        }
        if self.holdback_override is not None:
            env["HOLDBACK_DAYS_OVERRIDE"] = str(self.holdback_override)
        return env

    def redacted(self) -> dict[str, Any]:
        """What may be written into status.json: never a key, only its path."""
        return {
            "coordinator": self.coordinator,
            "programId": self.program_id,
            "lifeMint": self.life_mint,
            "targetsTable": self.targets_table,
            "payoutKeypairPath": self.payout_keypair,
            "validatorKeypairPath": self.validator_keypair,
            "anchorsPath": self.anchors_path,
            "anchorSource": self.anchor_source,
            "stateDir": self.state_dir,
            "genesisDay": self.genesis_day,
            "holdbackDaysOverride": self.holdback_override,
        }


# ── Worker client (status codes matter here, so this is not LogSource) ───────
@dataclass
class WorkerResponse:
    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return None


class WorkerClient:
    """Minimal GET client that REPORTS the status instead of raising on it.

    Discovery has to tell "day not archived" (404) and "day still open"
    (closed:false / 409) apart from a transport failure, so it cannot use
    LogSource, which collapses all of them into one exception.
    """

    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str) -> WorkerResponse:
        req = urllib.request.Request(f"{self.base_url}{path}",
                                     headers=ua_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return WorkerResponse(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            return WorkerResponse(exc.code, body)
        except (urllib.error.URLError, OSError) as exc:
            # Never echo the URL: it can carry credentials.
            raise PayoutError(
                f"GET {path} failed: {type(exc).__name__}") from None

    def log_manifest(self, day: int) -> WorkerResponse:
        return self.get(f"/log/{day_to_iso(day)}")

    def payout_envelope(self, day: int) -> WorkerResponse:
        return self.get(f"/payout/{day_to_iso(day)}")


# ── anchors (§9) ────────────────────────────────────────────────────────────
class AnchorBuilder:
    """Produces the day -> anchored logHash map the checker verifies against."""

    name = "abstract"

    def build(self, days: Iterable[int]) -> dict[int, str]:
        raise NotImplementedError


class WorkerAnchorBuilder(AnchorBuilder):
    """DEVNET ONLY.  Anchors taken from the Worker's own closed-day manifests.

    This is explicitly NOT an independent anchor and is gated on the hard-coded
    devnet program-id allow-list.  `build` raises AnchorSourceRefused for any
    other program id, so a real cluster can never be checked against anchors
    the coordinator supplied.
    """

    name = "worker"

    def __init__(self, client: WorkerClient, program_id: str, warn=print):
        self.client = client
        self.program_id = program_id
        self.warn = warn

    def build(self, days: Iterable[int]) -> dict[int, str]:
        if not C.is_devnet_program_id(self.program_id):
            raise AnchorSourceRefused(
                f"ANCHOR_SOURCE=worker builds PAYOUT_ANCHORS from the "
                f"coordinator's own manifests, which is not an independent "
                f"anchor; PROGRAM_ID={self.program_id} is not in the "
                f"hard-coded devnet allow-list "
                f"({', '.join(C.DEVNET_PROGRAM_IDS)}), so the service refuses "
                f"to run. Use ANCHOR_SOURCE=chain (not implemented yet) or a "
                f"file of independently obtained anchors.")
        self.warn(ANCHOR_WARNING)
        out: dict[int, str] = {}
        for day in days:
            resp = self.client.log_manifest(day)
            if resp.status != 200:
                continue
            obj = resp.json()
            if not isinstance(obj, dict):
                continue
            if obj.get("closed") is not True:
                continue
            log_hash = obj.get("log_hash")
            if isinstance(log_hash, str) and log_hash.strip():
                out[int(day)] = log_hash.strip().lower()
        return out


class FileAnchorBuilder(AnchorBuilder):
    """Anchors already on disk, obtained independently of the coordinator."""

    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def build(self, days: Iterable[int]) -> dict[int, str]:
        if not self.path.is_file():
            raise AnchorSourceRefused(
                f"ANCHOR_SOURCE=file but {self.path} does not exist")
        obj = json.loads(self.path.read_text())
        return {int(k): str(v).strip().lower() for k, v in obj.items()}


class ChainAnchorBuilder(AnchorBuilder):
    """PLACEHOLDER for the real thing: anchors read from the chain.

    Implement here: read the program's `anchor_hash` account for each day
    (BATCH_SPEC §9) through an RPC endpoint, and return day -> hash.  Nothing
    else in this module changes — `_anchor_builder` just hands back this class
    for ANCHOR_SOURCE=chain, and no program-id gate is needed because an
    on-chain anchor IS independent of the coordinator.
    """

    name = "chain"

    def build(self, days: Iterable[int]) -> dict[int, str]:
        raise AnchorSourceRefused(
            "ANCHOR_SOURCE=chain is not implemented yet: on-chain anchor_hash "
            "reads (BATCH_SPEC §9) still have to be wired up in "
            "payout.autorun.ChainAnchorBuilder.build")


def _anchor_builder(cfg: ServiceConfig, client: WorkerClient,
                    warn=print) -> AnchorBuilder:
    if cfg.anchor_source == "worker":
        return WorkerAnchorBuilder(client, cfg.program_id, warn=warn)
    if cfg.anchor_source == "file":
        return FileAnchorBuilder(cfg.anchors_path)
    return ChainAnchorBuilder()


# ── per-day outcome ─────────────────────────────────────────────────────────
OUTCOME_SIGNED = "signed_and_posted"
OUTCOME_POSTED_FROM_FILE = "posted_from_saved_file"
OUTCOME_SIGNED_POST_FAILED = "signed_post_failed"
OUTCOME_ALREADY = "already_signed"
OUTCOME_REFUSED = "refused"
OUTCOME_REFUSED_CACHED = "refused_unchanged"
OUTCOME_NO_BATCH = "no_batch"
OUTCOME_EMPTY = "empty_batch_no_signature_needed"
OUTCOME_OPEN = "day_open"
OUTCOME_NOT_ARCHIVED = "day_not_archived"
OUTCOME_ERROR = "error"

_ATTENTION = (OUTCOME_REFUSED, OUTCOME_REFUSED_CACHED,
              OUTCOME_SIGNED_POST_FAILED, OUTCOME_ERROR)


@dataclass
class DayOutcome:
    day: int
    date: str
    outcome: str
    batch_ids: list[str] = field(default_factory=list)
    rule: str | None = None
    reason: str | None = None
    signature_file: str | None = None
    posted: bool | None = None
    entry_count: int | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"day": self.day, "date": self.date,
                               "outcome": self.outcome}
        if self.batch_ids:
            out["batchIds"] = self.batch_ids
        if self.entry_count is not None:
            out["entryCount"] = self.entry_count
        if self.rule:
            out["rule"] = self.rule
        if self.reason:
            out["reason"] = self.reason
        if self.signature_file:
            out["signatureFile"] = self.signature_file
        if self.posted is not None:
            out["posted"] = self.posted
        if self.detail:
            out["detail"] = self.detail
        return out

    @property
    def needs_attention(self) -> bool:
        return self.outcome in _ATTENTION


def chunk_fingerprint(chunks: list[dict[str, Any]]) -> str:
    """A change detector over the batch AS SERVED.

    Deliberately NOT the protocol batch hash: that is recomputed from the
    entries by the verifier and must never be taken from the envelope.  This
    is only used to decide whether a batch the checker already refused has
    changed since, so any difference in the served bytes must move it.
    """
    body = json.dumps(chunks, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ── the service ─────────────────────────────────────────────────────────────
class PayoutCheckerService:
    """Unattended driver.  Owns no payout rule; calls the checker for those."""

    def __init__(self, cfg: ServiceConfig, *, run_ms: int,
                 client: WorkerClient | None = None,
                 routes: PayoutRoutes | None = None, log=None):
        # §2 / C5j: NOTHING in the `payout` package may read a clock, and
        # tests/test_payout_signer.py::test_the_payout_package_contains_no_wall_clock_call
        # enforces that over the executable source of every module here.  The
        # run instant is therefore INJECTED by the launcher outside the
        # package (payout_checker_runner.py) and is used only to bound the
        # day scan and to stamp the status file — it never reaches a payout
        # decision, which is made from the day number alone.
        self.run_ms = int(run_ms)
        self.cfg = cfg
        self.client = client or WorkerClient(cfg.coordinator, cfg.timeout)
        self.routes = routes or PayoutRoutes(cfg.coordinator, cfg.timeout)
        self._log_sink = log
        Path(cfg.work_dir).mkdir(parents=True, exist_ok=True)
        cfg.signatures_dir.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    # ── logging ─────────────────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        line = f"{_iso_utc(self.run_ms)} {msg}"
        print(line, flush=True)
        if self._log_sink is not None:
            self._log_sink(line)
        try:
            with self.cfg.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # ── service state (post-retry + standing refusals) ──────────────────────
    def _load_state(self) -> dict[str, Any]:
        p = self.cfg.service_state_path
        if p.is_file():
            try:
                obj = json.loads(p.read_text())
                if isinstance(obj, dict):
                    obj.setdefault("days", {})
                    return obj
            except json.JSONDecodeError:
                pass
        return {"days": {}}

    def _save_state(self) -> None:
        p = self.cfg.service_state_path
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        os.replace(tmp, p)

    def _day_state(self, day: int) -> dict[str, Any]:
        return self.state["days"].setdefault(str(int(day)), {})

    # ── discovery ───────────────────────────────────────────────────────────
    def last_day(self) -> int:
        """Upper bound of the scan.

        The CHECKER reads no clock (BATCH_SPEC §2 / C5j) and that property is
        preserved: the clock is used only here, by the scheduler, to decide how
        far to look, and never reaches a payout decision.
        """
        if self.cfg.last_day is not None:
            return int(self.cfg.last_day)
        return self.run_ms // C.MS_PER_DAY

    def discover(self) -> list[tuple[int, dict[str, Any] | None, int]]:
        """[(day, manifest_or_None, status)] for the whole scan range."""
        out = []
        for day in range(int(self.cfg.first_day), self.last_day() + 1):
            resp = self.client.log_manifest(day)
            obj = resp.json() if resp.status == 200 else None
            out.append((day, obj if isinstance(obj, dict) else None, resp.status))
        return out

    # ── anchors ─────────────────────────────────────────────────────────────
    def refresh_anchors(self, through_day: int) -> dict[int, str]:
        """Write PAYOUT_ANCHORS covering genesis_day..through_day."""
        builder = _anchor_builder(self.cfg, self.client, warn=self.log)
        days = range(int(self.cfg.genesis_day), int(through_day) + 1)
        anchors = builder.build(days)
        if builder.name != "file":
            p = Path(self.cfg.anchors_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({str(k): v for k, v in sorted(anchors.items())}))
        missing = [d for d in days if d not in anchors]
        if missing:
            self.log(f"NOTE: no anchor for day(s) {missing} (not closed / not "
                     f"archived); the checker will refuse any day that needs "
                     f"them")
        return anchors

    # ── posting ─────────────────────────────────────────────────────────────
    def _post_file(self, day: int, path: Path) -> tuple[bool, str]:
        """POST the saved file's EXACT bytes.  Returns (ok, detail)."""
        body = path.read_bytes()
        try:
            resp = self.routes.post_signature(day, body)
        except PayoutError as exc:
            return False, str(exc)
        return True, json.dumps(resp, sort_keys=True)

    # ── signing (delegated to payout.cli, unchanged) ────────────────────────
    def _run_checker(self, day: int, out_path: Path) -> tuple[int, str]:
        """Run `payout.cli sign` for one day.  Returns (exit_code, output)."""
        args = argparse.Namespace(
            coordinator=self.cfg.coordinator,
            day=int(day),
            out=str(out_path),
            post=False,                      # the service posts, not the CLI
            timeout=self.cfg.timeout,
            genesis_day=int(self.cfg.genesis_day),
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = payout_cli.cmd_sign(args, self.cfg.checker_env())
        return rc, buf.getvalue()

    def _refusal_from(self, output: str, seq_before: int
                      ) -> tuple[str | None, str | None]:
        """The rule + reason for a refusal: ledger record first, text second."""
        try:
            ledger = DecisionLedger(self.cfg.state_dir)
            for rec in ledger.decisions():
                if int(rec.get("seq") or 0) <= seq_before:
                    continue
                if rec.get("verdict") != "refuse":
                    continue
                findings = rec.get("findings") or []
                msg = findings[0].get("message") if findings else None
                return rec.get("rule"), msg
        except Exception:
            pass
        m = _REFUSED_RE.search(output or "")
        if m:
            return m.group(2), (output or "").strip().splitlines()[-1]
        return None, (output or "").strip() or None

    def _ledger_seq(self) -> int:
        try:
            obj = json.loads(
                (Path(self.cfg.state_dir) / "payout_index.json").read_text())
            return int(obj.get("decision_seq") or 0)
        except Exception:
            return 0

    def _signed_batch_ids(self) -> set[int]:
        try:
            return DecisionLedger(self.cfg.state_dir).signed_batch_ids
        except Exception:
            return set()

    # ── one day ─────────────────────────────────────────────────────────────
    def process_day(self, day: int, manifest: dict[str, Any] | None,
                    status: int) -> DayOutcome:
        iso = day_to_iso(day)
        st = self._day_state(day)

        # (2) a signature file that was written but never accepted: re-post it
        # and NEVER sign again.
        sig_path = self.cfg.signatures_dir / f"{iso}.json"
        if st.get("signatureFile") and not st.get("posted") and sig_path.is_file():
            ok, detail = self._post_file(day, sig_path)
            st["posted"] = ok
            st["lastPostDetail"] = detail
            self._save_state()
            if ok:
                self.log(f"day {day} ({iso}): re-posted the SAVED signature "
                         f"file, no new signature — {detail}")
                return DayOutcome(day, iso, OUTCOME_POSTED_FROM_FILE,
                                  batch_ids=list(st.get("batchIds") or []),
                                  signature_file=str(sig_path), posted=True,
                                  detail=detail)
            self.log(f"day {day} ({iso}): POST of the saved file failed again, "
                     f"file kept — {detail}")
            return DayOutcome(day, iso, OUTCOME_SIGNED_POST_FAILED,
                              batch_ids=list(st.get("batchIds") or []),
                              signature_file=str(sig_path), posted=False,
                              detail=detail)

        if status == 404:
            return DayOutcome(day, iso, OUTCOME_NOT_ARCHIVED)
        if manifest is None:
            return DayOutcome(day, iso, OUTCOME_ERROR,
                              detail=f"GET /log/{iso} returned HTTP {status}")
        if manifest.get("closed") is not True:
            return DayOutcome(day, iso, OUTCOME_OPEN,
                              detail=f"closed={manifest.get('closed')!r}")

        # (5) is there a batch at all?
        resp = self.client.payout_envelope(day)
        if resp.status == 404:
            return DayOutcome(day, iso, OUTCOME_NO_BATCH,
                              detail="GET /payout/%s returned HTTP 404" % iso)
        if resp.status != 200:
            return DayOutcome(day, iso, OUTCOME_ERROR,
                              detail=f"GET /payout/{iso} returned HTTP "
                                     f"{resp.status}")
        envelope = resp.json()
        if not isinstance(envelope, dict):
            return DayOutcome(day, iso, OUTCOME_ERROR,
                              detail=f"GET /payout/{iso} returned a non-object")
        chunks = envelope.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            return DayOutcome(day, iso, OUTCOME_NO_BATCH,
                              detail="envelope carries no chunks[]")
        batch_ids = [str(c.get("batchId")) for c in chunks
                     if isinstance(c, dict)]
        entry_count = sum(len(c.get("entries") or []) for c in chunks
                          if isinstance(c, dict))
        if entry_count == 0:
            self.log(f"day {day} ({iso}): batch {batch_ids} carries zero "
                     f"entries — no signature needed")
            st["batchIds"] = batch_ids
            st["emptyBatch"] = True
            self._save_state()
            return DayOutcome(day, iso, OUTCOME_EMPTY, batch_ids=batch_ids,
                              entry_count=0,
                              detail="no payable entries, nothing to sign")

        fingerprint = chunk_fingerprint(chunks)

        # (3) already in the ledger: never offered to the signer again.
        signed = self._signed_batch_ids()
        done = [b for b in batch_ids if b.isdigit() and int(b) in signed]
        if done:
            return DayOutcome(day, iso, OUTCOME_ALREADY, batch_ids=batch_ids,
                              entry_count=entry_count,
                              detail=f"batchId(s) {done} already in "
                                     f"{self.cfg.state_dir}/payout_index.json")

        # (4) a standing refusal for an UNCHANGED batch: do not re-verify.
        prior = st.get("refusal")
        if prior and prior.get("fingerprint") == fingerprint:
            return DayOutcome(day, iso, OUTCOME_REFUSED_CACHED,
                              batch_ids=batch_ids, entry_count=entry_count,
                              rule=prior.get("rule"),
                              reason=prior.get("reason"),
                              detail="batch unchanged since the refusal; not "
                                     "retried until its fingerprint changes")

        # (6) anchors, then the checker itself.
        try:
            self.refresh_anchors(day)
        except AnchorSourceRefused:
            raise
        except PayoutError as exc:
            return DayOutcome(day, iso, OUTCOME_ERROR,
                              detail=f"could not build anchors: {exc}")

        seq_before = self._ledger_seq()
        rc, output = self._run_checker(day, sig_path)
        for line in (output or "").strip().splitlines():
            self.log(f"  [checker day {day}] {line}")

        if rc == payout_cli.EXIT_CONFIG:
            return DayOutcome(day, iso, OUTCOME_ERROR, batch_ids=batch_ids,
                              detail=(output or "").strip().splitlines()[-1]
                              if output.strip() else "checker refused to start")
        if rc != payout_cli.EXIT_OK:
            rule, reason = self._refusal_from(output, seq_before)
            st["refusal"] = {"rule": rule, "reason": reason,
                             "fingerprint": fingerprint,
                             "batchIds": batch_ids}
            st.pop("signatureFile", None)
            st.pop("posted", None)
            self._save_state()
            self.log(f"day {day} ({iso}): REFUSED — rule={rule}")
            return DayOutcome(day, iso, OUTCOME_REFUSED, batch_ids=batch_ids,
                              entry_count=entry_count, rule=rule,
                              reason=reason)

        if not sig_path.is_file():
            return DayOutcome(day, iso, OUTCOME_ERROR, batch_ids=batch_ids,
                              detail="checker reported ACCEPTED but wrote no "
                                     "signature file")
        st.pop("refusal", None)
        st["batchIds"] = batch_ids
        st["signatureFile"] = str(sig_path)
        st["fingerprint"] = fingerprint
        st["posted"] = False
        self._save_state()

        ok, detail = self._post_file(day, sig_path)
        st["posted"] = ok
        st["lastPostDetail"] = detail
        self._save_state()
        if ok:
            self.log(f"day {day} ({iso}): SIGNED and POSTED {batch_ids} — "
                     f"{detail}")
            return DayOutcome(day, iso, OUTCOME_SIGNED, batch_ids=batch_ids,
                              entry_count=entry_count,
                              signature_file=str(sig_path), posted=True,
                              detail=detail)
        self.log(f"day {day} ({iso}): signed, but the POST failed — file kept "
                 f"at {sig_path} for the next run — {detail}")
        return DayOutcome(day, iso, OUTCOME_SIGNED_POST_FAILED,
                          batch_ids=batch_ids, entry_count=entry_count,
                          signature_file=str(sig_path), posted=False,
                          detail=detail)

    # ── one run ─────────────────────────────────────────────────────────────
    def run_once(self) -> tuple[int, dict[str, Any]]:
        started = self.run_ms
        self.log(f"run start: coordinator={self.cfg.coordinator} "
                 f"programId={self.cfg.program_id} "
                 f"anchorSource={self.cfg.anchor_source} "
                 f"days={self.cfg.first_day}..{self.last_day()}")
        if self.cfg.anchor_source == "worker":
            # Loud, every run, before anything else happens.
            self.log(ANCHOR_WARNING)
            if not C.is_devnet_program_id(self.cfg.program_id):
                detail = (f"ANCHOR_SOURCE=worker is refused for "
                          f"PROGRAM_ID={self.cfg.program_id}: it is not in the "
                          f"hard-coded devnet allow-list, and anchors taken "
                          f"from the coordinator are not independent")
                self.log(f"REFUSING TO RUN: {detail}")
                return self._finish(EXIT_CONFIG, [], started, error=detail)

        outcomes: list[DayOutcome] = []
        try:
            discovered = self.discover()
        except PayoutError as exc:
            self.log(f"REFUSING TO RUN: discovery failed: {exc}")
            return self._finish(EXIT_CONFIG, [], started, error=str(exc))

        for day, manifest, status in discovered:
            try:
                outcome = self.process_day(day, manifest, status)
            except AnchorSourceRefused as exc:
                self.log(f"REFUSING TO RUN: {exc}")
                return self._finish(EXIT_CONFIG, outcomes, started,
                                    error=str(exc))
            except PayoutError as exc:
                outcome = DayOutcome(day, day_to_iso(day), OUTCOME_ERROR,
                                     detail=str(exc))
            except Exception as exc:                      # never crash a run
                outcome = DayOutcome(day, day_to_iso(day), OUTCOME_ERROR,
                                     detail=f"{type(exc).__name__}: {exc}")
            if outcome.outcome not in (OUTCOME_NOT_ARCHIVED,):
                outcomes.append(outcome)

        rc = EXIT_ATTENTION if any(o.needs_attention for o in outcomes) else EXIT_OK
        return self._finish(rc, outcomes, started)

    def _finish(self, rc: int, outcomes: list[DayOutcome], started: int,
                error: str | None = None) -> tuple[int, dict[str, Any]]:
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.outcome] = counts.get(o.outcome, 0) + 1
        status = {
            "runAt": _iso_utc(started),
            "exitCode": rc,
            "needsAttention": bool(rc),
            "config": self.cfg.redacted(),
            "scan": {"firstDay": self.cfg.first_day, "lastDay": self.last_day()},
            "anchorWarning": (ANCHOR_WARNING
                              if self.cfg.anchor_source == "worker" else None),
            "counts": counts,
            "days": [o.as_dict() for o in outcomes],
        }
        if error:
            status["error"] = error
        p = self.cfg.status_path
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2, sort_keys=True))
        os.replace(tmp, p)
        self.log(f"run end: exit={rc} counts={json.dumps(counts, sort_keys=True)} "
                 f"status={p}")
        return rc, status


# ── entry point ─────────────────────────────────────────────────────────────
DEFAULT_CONFIG = "/root/payout-work/service.env"


def main(argv: list[str] | None = None, *, run_ms: int | None = None) -> int:
    """Entry point.  `run_ms` MUST be supplied by the launcher.

    payload_checker_runner.py at the repo root is that launcher: it is outside
    the `payout` package, so it is allowed to read the system clock.  Calling
    this without `run_ms` is a configuration error rather than a silent
    fallback to a clock inside the package.
    """
    parser = argparse.ArgumentParser(
        prog="python -m payout.autorun",
        description="Unattended payout-checker run: sign and post every "
                    "closed day that has an unsigned batch.")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"KEY=VALUE settings file (default {DEFAULT_CONFIG})")
    parser.add_argument("--allow-insecure-config", action="store_true",
                        help="permit a config file that is not mode 0600")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if run_ms is None:
        print("REFUSING TO START: no run instant was supplied; start this "
              "service through payout_checker_runner.py, which owns the clock "
              "(the payout package itself must read none)", file=sys.stderr)
        return EXIT_CONFIG

    try:
        env = load_env_file(
            args.config, require_owner_only=not args.allow_insecure_config)
        cfg = ServiceConfig.from_mapping(env)
    except (ServiceConfigError, ValueError) as exc:
        print(f"REFUSING TO START: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    # One run at a time: the PM2 schedule must never overlap itself.
    Path(cfg.work_dir).mkdir(parents=True, exist_ok=True)
    lock_path = Path(cfg.work_dir) / ".autorun.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another payout.autorun is still running; exiting quietly",
                  file=sys.stderr)
            return EXIT_OK
        service = PayoutCheckerService(cfg, run_ms=run_ms)
        rc, _status = service.run_once()
        return rc
