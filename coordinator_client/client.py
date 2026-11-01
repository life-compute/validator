"""Coordinator HTTP client and the validator poll loop.

Flow
----
  GET /results/pending  (signed)
    -> for each row not already terminal in the progress store:
         claim (committed to disk BEFORE work)
         re-score with the lease's scorer profile, fail closed on inputs
         POST /verdicts (signed) with score + error + input-hash record
         or POST a validator_error with a contract reason code
       mark terminal (committed) ONLY for an accepted verdict or a 409

Who decides "done"
------------------
Only two outcomes retire a result locally: a verdict the Worker ACCEPTED
(2xx), and a 409 answer meaning a verdict already exists.  A validator_error
and a malformed row are NOT terminal — the Worker owns the retry decision
(soft-error cooldown + review window) and re-offers the result itself.
A log-only (pegRNA) item is terminal: it is a final local disposition that
posts no verdict, so nothing else would ever stop it being re-scored.

Guarantees
----------
* 5xx reads retry with capped exponential backoff, bounded attempts.
* No item is retried forever: MAX_ITEM_ATTEMPTS locally, max_attempts
  coordinator-side.
* Progress is on disk, so a restart never re-scores a finished result.
* Exactly one startup line names the coordinator HOST.  No token, no key, no
  query string is ever logged or placed in argv.
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfg
from . import contract, hashpin
from .bias import BiasTable
from .contract import ContractError, PendingResult
from .progress import ProgressStore
from .rescore import RescoreFailed, Rescorer, RescoreTimeout
from .signing import Signer

import sys as _sys
from pathlib import Path as _Path

# Repo-root module: the single source of truth for the outbound User-Agent.
_repo_root = str(_Path(__file__).resolve().parent.parent)
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)
from http_ua import ua_headers  # noqa: E402

log = logging.getLogger("life-validator.client")


def _as_int(val: Any) -> int:
    """Coerce a Worker-supplied attempt counter to an int, defaulting to 0.

    Used only to build the offer tag, never to score: a junk value simply
    means "offer 0", which is the conservative choice (same budget).
    """
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    body: dict[str, Any]


class CoordinatorClient:
    """Signed HTTP transport for the coordinator API."""

    def __init__(self, base_url: str, signer: Signer, *,
                 timeout: int = 30,
                 max_attempts: int = 5,
                 backoff_base: float = 2.0,
                 backoff_cap: float = 60.0,
                 opener=None):
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._opener = opener or urllib.request.urlopen

    # ── transport ───────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body_obj: Any | None = None,
                 signed: bool = True) -> Response:
        from .signing import canonical_body

        body_bytes = canonical_body(body_obj)
        # User-Agent is mandatory on every outbound request: the coordinator's
        # Cloudflare edge rejects the default Python-urllib token with
        # 403 "error code: 1010" before X-Auth is evaluated.
        headers = ua_headers({"Content-Type": "application/json"})
        if signed:
            # X-Auth is built fresh per attempt by the caller's retry loop so
            # each retry gets a new nonce and timestamp (the coordinator
            # rejects a replayed nonce and a stale timestamp).
            headers["X-Auth"] = self.signer.auth_header(method, path, body_bytes)

        req = urllib.request.Request(
            self.base_url + path,
            data=body_bytes if method != "GET" else None,
            headers=headers, method=method)
        try:
            with self._opener(req, timeout=self.timeout) as r:
                raw = r.read()
                status = getattr(r, "status", 200)
        except urllib.error.HTTPError as e:
            raw = e.read()
            status = e.code
            try:
                parsed = json.loads(raw or b"{}")
            except Exception:
                parsed = {"error": (raw or b"").decode(errors="replace")}
            raise HttpError(status, json.dumps(parsed)) from None

        try:
            parsed = json.loads(raw or b"{}")
        except Exception:
            parsed = {}
        return Response(status, parsed)

    def _with_retry(self, method: str, path: str, body_obj: Any | None = None,
                    signed: bool = True) -> Response:
        """Retry 5xx and transport errors with capped exponential backoff.

        4xx is NOT retried — it is a contract/auth problem, not a blip.
        Attempts are bounded; we never spin forever on one item.
        """
        delay = self.backoff_base
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._request(method, path, body_obj, signed)
            except HttpError as e:
                last = e
                if e.status < 500 and e.status != 429:
                    raise
            except Exception as e:
                last = e
            if attempt >= self.max_attempts:
                break
            sleep_for = min(delay, self.backoff_cap)
            # Full jitter, so a fleet of validators does not thunder.
            sleep_for = random.uniform(0, sleep_for)
            log.warning("%s %s failed (attempt %d/%d) — retrying in %.1fs",
                        method, path, attempt, self.max_attempts, sleep_for)
            time.sleep(sleep_for)
            delay *= 2
        assert last is not None
        raise last

    # ── endpoints ───────────────────────────────────────────────────────────
    def get_config(self) -> dict[str, Any]:
        try:
            return self._with_retry("GET", contract.PATH_CONFIG, signed=False).body
        except Exception as e:
            log.warning("GET /config failed (%s) — using contract defaults", e)
            return dict(contract.DEFAULT_CONFIG)

    def get_pending(self) -> list[dict[str, Any]]:
        resp = self._with_retry("GET", contract.PATH_PENDING)
        return list(resp.body.get("pending") or [])

    def post_verdict(self, body: dict[str, Any]) -> Response:
        return self._with_retry("POST", contract.PATH_VERDICTS, body)


class ValidatorService:
    """The poll → re-score → verdict loop."""

    def __init__(self, client: CoordinatorClient, rescorer: Rescorer,
                 store: ProgressStore, bias: BiasTable, *,
                 coord_config: dict[str, Any] | None = None,
                 gpu_model: str = "",
                 max_item_attempts: int = 3,
                 verdict_log: Path | None = None):
        self.client = client
        self.rescorer = rescorer
        self.store = store
        self.bias = bias
        self.config = coord_config or dict(contract.DEFAULT_CONFIG)
        self.gpu_model = gpu_model
        self.max_item_attempts = max_item_attempts
        self.verdict_log = verdict_log

    # ── one pending batch ───────────────────────────────────────────────────
    def process_batch(self) -> dict[str, int]:
        stats = {"seen": 0, "skipped_done": 0, "verdicts": 0,
                 "errors": 0, "log_only": 0, "malformed": 0}
        for raw in self.client.get_pending():
            stats["seen"] += 1
            rid = raw.get("id")
            # The Worker's own attempt counter identifies the offer.  Read it
            # from the raw row: the skip decision happens before parsing.
            offer_tag = str(_as_int(raw.get("scoring_attempts")))

            if isinstance(rid, int) and self.store.is_done(rid, offer_tag):
                stats["skipped_done"] += 1
                continue

            try:
                item = PendingResult.from_dict(raw)
            except ContractError as e:
                # The row does not satisfy the contract we built to.  Do not
                # guess any input — report and move on.  NOT marked done: a
                # malformed row is the Worker's to fix and re-offer; if we
                # retired it locally we would never see the corrected row.
                stats["malformed"] += 1
                log.error("result %s: %s", rid, e)
                if isinstance(rid, int):
                    self._post_error(rid, contract.REASON_MISSING_CONTRACT_FIELD, str(e))
                continue

            # Same offer tag as the skip check above, so a `failed` row and a
            # fresh claim can never disagree about which offer this is.
            attempts = self.store.claim(item.id, item.molecule_hash,
                                        offer_tag=offer_tag)
            if attempts > self.max_item_attempts:
                log.error("result %d: local attempt cap (%d) reached for this "
                          "offer (worker attempt %d) — giving up on this offer",
                          item.id, self.max_item_attempts, item.scoring_attempts)
                self.store.mark_failed(item.id, "attempt_cap")
                self._post_error(item.id, contract.REASON_INTERNAL_ERROR,
                                 "local attempt cap reached")
                stats["errors"] += 1
                continue

            outcome = self._handle(item)
            stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def _handle(self, item: PendingResult) -> str:
        try:
            result = self.rescorer.rescore(item)
        except hashpin.InputPinError as e:
            log.error("result %d (%s): %s", item.id, item.target, e.detail)
            self._post_error(item.id, e.reason_code, e.detail)
            return "errors"
        except RescoreTimeout as e:
            log.error("result %d: %s", item.id, e)
            self._post_error(item.id, contract.REASON_TIMEOUT, str(e))
            return "errors"
        except RescoreFailed as e:
            log.error("result %d: %s", item.id, e.detail)
            self._post_error(item.id, e.reason_code, e.detail)
            return "errors"
        except Exception as e:  # noqa: BLE001 — never let one item kill the loop
            log.exception("result %d: unexpected re-score failure", item.id)
            self._post_error(item.id, contract.REASON_INTERNAL_ERROR, str(e))
            return "errors"

        if result.log_only:
            # pegRNA: recorded, never submitted as a verdict.  Terminal by
            # local decision — no verdict is ever posted for it, so the
            # accepted-verdict rule cannot apply (see module docstring).
            self._write_log({"result_id": item.id, "modality": item.modality,
                             "mode": "LOG_ONLY", "validator_score": result.score,
                             "claimed": item.score,
                             "input_hashes": result.input_hashes.as_dict()})
            self.store.mark_submitted(item.id, "log_only")
            return "log_only"

        # Sanity gate — mirrors the coordinator's own range check so we do not
        # waste a coordinator attempt on a value it will reject outright.
        lo, hi = contract.sanity_range(self.config, item.modality)
        if not (lo <= result.score <= hi):
            detail = f"rescored {result.score:.4f} outside sanity [{lo}, {hi}]"
            log.error("result %d: %s", item.id, detail)
            self._post_error(item.id, contract.REASON_INTERNAL_ERROR, detail)
            return "errors"

        bias_factor = self.bias.factor(self.gpu_model, item.modality)
        adjusted = item.score * bias_factor if bias_factor is not None else item.score
        tolerance = contract.tolerance_for(self.config, item.modality,
                                           bias_factor is not None)
        decision, rel_err = contract.coord_decision(result.score, adjusted, tolerance)

        body = contract.verdict_body(
            item.id, result.score, self.gpu_model or None, decision,
            input_hashes=result.input_hashes.as_dict())
        try:
            resp = self.client.post_verdict(body)
        except HttpError as e:
            if e.status == 409:
                # 409 Conflict = the Worker already holds a verdict for this
                # result.  The verdict EXISTS, so this is terminal success:
                # mark done and never spend GPU time on it again.
                log.info("result %d: coordinator answered 409 — a verdict "
                         "already exists, marking done", item.id)
                self.store.mark_submitted(item.id, "already_recorded:409")
                return "already_recorded"
            log.error("result %d: POST /verdicts failed: %s", item.id, e)
            return "errors"
        except Exception as e:
            log.error("result %d: POST /verdicts failed: %s", item.id, e)
            return "errors"

        # Only a verdict the Worker accepted (2xx) is terminal.
        self.store.mark_submitted(item.id, decision)
        self._write_log({"result_id": item.id, "modality": item.modality,
                         "target": item.target, "claimed": item.score,
                         "adjusted_claimed": adjusted,
                         "validator_score": result.score,
                         "rel_err": round(rel_err, 6), "tolerance": tolerance,
                         "bias_factor": bias_factor,
                         "validator_verdict": decision,
                         "elapsed_s": round(result.elapsed_s, 3),
                         "input_hashes": result.input_hashes.as_dict(),
                         "coordinator": resp.body})
        log.info("result %d %s  claimed=%.4f adj=%.4f rescored=%.4f rel_err=%.4f tol=%.2f",
                 item.id, decision.upper(), item.score, adjusted, result.score,
                 rel_err, tolerance)
        return "verdicts"

    # ── helpers ─────────────────────────────────────────────────────────────
    def _post_error(self, result_id: int, reason_code: str, detail: str) -> bool:
        """POST a validator_error.  Returns True iff a 409 proved a verdict exists.

        A validator_error is NOT terminal and must NOT be marked done: the
        Worker owns the retry decision (soft-error cooldown + review window).
        Marking it done here is what made every re-offer come back as
        `skipped_done`, so the result was never re-scored.  The only terminal
        answer on this path is a 409 ("a verdict already exists").
        """
        already = False
        try:
            self.client.post_verdict(contract.error_body(result_id, reason_code))
        except HttpError as e:
            if e.status == 409:
                log.info("result %d: 409 on validator_error — a verdict "
                         "already exists, marking done", result_id)
                self.store.mark_submitted(result_id, "already_recorded:409")
                already = True
            else:
                log.error("result %d: POST validator_error failed: %s", result_id, e)
        except Exception as e:
            log.error("result %d: POST validator_error failed: %s", result_id, e)
        self._write_log({"result_id": result_id, "status": "validator_error",
                         "reason_code": reason_code, "detail": detail})
        return already

    def _write_log(self, row: dict[str, Any]) -> None:
        if not self.verdict_log:
            return
        try:
            self.verdict_log.parent.mkdir(parents=True, exist_ok=True)
            row.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            with self.verdict_log.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass  # telemetry must never break the loop

    # ── long-running loop ───────────────────────────────────────────────────
    def run_forever(self, poll_seconds: int = 30) -> None:
        while True:
            try:
                stats = self.process_batch()
                if stats["seen"]:
                    log.info("batch: %s", stats)
            except Exception as e:  # coordinator outage — back off, keep going
                log.warning("poll failed (%s) — retrying in %ds", e, poll_seconds)
            time.sleep(poll_seconds)


# ── startup ──────────────────────────────────────────────────────────────────

def build_service(*, runner, signer: Signer | None = None,
                  store: ProgressStore | None = None) -> ValidatorService:
    """Wire everything from config.  Emits the single startup line."""
    signer = signer or Signer.from_keypair_file(cfg.VALIDATOR_KEYPAIR, cfg.WORKER_NAME)

    # Exactly one startup line naming the coordinator HOST.  host_only()
    # strips any path/query, so no token can appear here.
    log.info("coordinator: %s  validator wallet %s",
             cfg.host_only(cfg.COORDINATOR_URL), signer.wallet)

    client = CoordinatorClient(cfg.COORDINATOR_URL, signer,
                               timeout=cfg.HTTP_TIMEOUT,
                               max_attempts=cfg.READ_MAX_ATTEMPTS,
                               backoff_base=cfg.READ_BACKOFF_BASE,
                               backoff_cap=cfg.READ_BACKOFF_CAP)
    coord_config = client.get_config()
    store = store or ProgressStore(cfg.PROGRESS_DB)
    bias = BiasTable.from_audit(cfg.OUTPUT_DIR / "validator_audit.jsonl",
                                cfg.GPU_ATTRIBUTION_CUTOFF_UTC,
                                min_samples=cfg.GPU_BIAS_MIN_SAMPLES,
                                lo=cfg.BIAS_FACTOR_LO, hi=cfg.BIAS_FACTOR_HI)
    rescorer = Rescorer(runner, msa_dir=cfg.MSA_DIR, seed=cfg.BOLTZ_SEED,
                        num_subsampled_msa=cfg.NUM_SUBSAMPLED_MSA,
                        timeout_s=cfg.RESCORE_TIMEOUT_S)
    return ValidatorService(client, rescorer, store, bias,
                            coord_config=coord_config,
                            max_item_attempts=cfg.MAX_ITEM_ATTEMPTS,
                            verdict_log=cfg.VERDICT_LOG)
