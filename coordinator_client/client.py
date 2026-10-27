"""Coordinator HTTP client and the validator poll loop.

Flow
----
  GET /results/pending  (signed)
    -> for each row not already terminal in the progress store:
         claim (committed to disk BEFORE work)
         re-score with the lease's scorer profile, fail closed on inputs
         POST /verdicts (signed) with score + error + input-hash record
         or POST a validator_error with a contract reason code
       mark terminal (committed)

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

log = logging.getLogger("life-validator.client")


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
        headers = {"Content-Type": "application/json"}
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

            if isinstance(rid, int) and self.store.is_done(rid):
                stats["skipped_done"] += 1
                continue

            try:
                item = PendingResult.from_dict(raw)
            except ContractError as e:
                # The row does not satisfy the contract we built to.  Do not
                # guess any input — report and move on.
                stats["malformed"] += 1
                log.error("result %s: %s", rid, e)
                if isinstance(rid, int):
                    self._post_error(rid, contract.REASON_MISSING_CONTRACT_FIELD, str(e))
                continue

            attempts = self.store.claim(item.id, item.molecule_hash)
            if attempts > self.max_item_attempts:
                log.error("result %d: local attempt cap (%d) reached — giving up",
                          item.id, self.max_item_attempts)
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
            # pegRNA: recorded, never submitted as a verdict.
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
        except Exception as e:
            log.error("result %d: POST /verdicts failed: %s", item.id, e)
            return "errors"

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
    def _post_error(self, result_id: int, reason_code: str, detail: str) -> None:
        try:
            self.client.post_verdict(contract.error_body(result_id, reason_code))
            self.store.mark_submitted(result_id, f"validator_error:{reason_code}")
        except Exception as e:
            log.error("result %d: POST validator_error failed: %s", result_id, e)
        self._write_log({"result_id": result_id, "status": "validator_error",
                         "reason_code": reason_code, "detail": detail})

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
