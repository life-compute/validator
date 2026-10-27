"""Mock coordinator — built from /root/api/API_CONTRACT.md, not from the client.

Implements the subset the validator talks to:
  GET  /config          (public)
  GET  /health          (public)
  GET  /results/pending (validator, signed)
  POST /verdicts        (validator, signed)

Auth verification follows §1 exactly: X-Auth JSON header, canonical message
METHOD\\nPATH\\nbody_sha256\\nnonce\\ntimestamp, Ed25519 over base58 wallet,
5-minute nonce window, 5-minute clock skew, replay rejection.

Verdict handling follows §3.2: corrupted-score 422, sanity range, protein
bias adjustment, rel_err, coord_decision, hit gate, disagree, attempt state
machine.

Nothing here imports the client's signing code — the contract test is only
meaningful if both sides were written independently from the document.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import base58
from solders.pubkey import Pubkey
from solders.signature import Signature

NONCE_WINDOW_S = 300
CLOCK_SKEW_S = 300
MAX_BODY = 16 * 1024

CONFIG: dict[str, Any] = {
    "version": "2.0.0",
    "modality_tolerance": {
        "protein":        {"tolerance_with_bias": 0.25, "tolerance_without_bias": 0.35,
                           "sanity_min": -16.0, "sanity_max": 0.0},
        "mrna":           {"tolerance_with_bias": 0.10, "tolerance_without_bias": 0.10,
                           "sanity_min": -9.5, "sanity_max": -5.5},
        "crispr":         {"tolerance_with_bias": 0.10, "tolerance_without_bias": 0.10,
                           "sanity_min": -9.5, "sanity_max": -5.5},
        "small_molecule": {"tolerance_with_bias": 0.10, "tolerance_without_bias": 0.10,
                           "sanity_min": -16.0, "sanity_max": 0.0},
    },
    "target_hit_thresholds": {"CDK4": -9.0, "MYC": None},
    "no_threshold_targets": ["MYC"],
    "enforce_membership": False,
    "bias_guard": {"min_samples": 10, "min_wallets": 3, "min_factor": 0.85,
                   "max_factor": 1.15, "max_daily_delta": 0.02,
                   "max_weekly_drift": 0.05, "update_interval_ms": 86400000},
    "reject_rate": {"window": 50, "threshold": 0.10},
    "scoring": {"rescore_timeout_ms": 1_200_000, "reclaim_timeout_ms": 1_500_000,
                "max_attempts": 3},
    "scorer_profile": {"boltz_version": "1.0.1",
                       "flags": ["--use-msa-server", "--recycling-steps=3"],
                       "alignment_sampling": "uniform"},
}


class MockState:
    """Server-side state: pending rows, nonces, verdicts, attempt counters."""

    def __init__(self, validators: set[str] | None = None):
        self.lock = threading.Lock()
        self.pending: dict[int, dict[str, Any]] = {}
        self.verdicts: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.nonces: dict[str, float] = {}
        self.validators: set[str] = validators if validators is not None else set()
        self.unscorable: set[int] = set()
        self.request_log: list[tuple[str, str]] = []
        # Fault injection
        self.fail_next_pending: int = 0      # N consecutive 503s on GET /pending
        self.fail_next_verdict: int = 0
        self.bias_factor: float | None = None   # protein only, when set

    def add_pending(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.pending[int(row["id"])] = dict(row)

    def open_validator_registration(self, wallet: str) -> None:
        with self.lock:
            self.validators.add(wallet)


class _Handler(BaseHTTPRequestHandler):
    state: MockState = None  # type: ignore[assignment]

    # silence the stdlib access log
    def log_message(self, format, *args):  # noqa: A002, D102
        pass

    # ── helpers ─────────────────────────────────────────────────────────────
    def _send(self, status: int, obj: Any) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _auth(self, method: str, path: str, body: bytes) -> tuple[str | None, str | None]:
        """Return (wallet, error_message) per contract §1."""
        hdr = self.headers.get("X-Auth")
        if not hdr:
            return None, "Missing X-Auth"
        try:
            auth = json.loads(hdr)
        except Exception:
            return None, "Malformed X-Auth"

        for field in ("wallet", "nonce", "timestamp", "signature"):
            if field not in auth:
                return None, f"Missing {field} in X-Auth"

        wallet = auth["wallet"]
        try:
            pub_bytes = base58.b58decode(wallet)
        except Exception:
            return None, "Invalid wallet address"
        if len(pub_bytes) != 32:
            return None, "Invalid wallet address"

        try:
            ts = int(auth["timestamp"])
        except Exception:
            return None, "Timestamp out of range"
        if abs(int(time.time()) - ts) > CLOCK_SKEW_S:
            return None, "Timestamp out of range"

        nonce = str(auth["nonce"])
        if not nonce:
            return None, "Missing nonce"
        now = time.time()
        with self.state.lock:
            # GC expired nonces, then check replay.
            for k in [k for k, v in self.state.nonces.items() if now - v > NONCE_WINDOW_S]:
                del self.state.nonces[k]
            if nonce in self.state.nonces:
                return None, "Replay: nonce already used"
            self.state.nonces[nonce] = now

        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{method}\n{path}\n{body_hash}\n{nonce}\n{ts}".encode()
        try:
            sig = Signature.from_bytes(bytes.fromhex(auth["signature"]))
            if not sig.verify(Pubkey.from_bytes(pub_bytes), message):
                return None, "Invalid signature"
        except Exception:
            return None, "Invalid signature"

        return wallet, None

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._err(413, "Request body exceeds 16 KB limit")
            return None
        return self.rfile.read(length) if length else b""

    # ── routes ──────────────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        with self.state.lock:
            self.state.request_log.append(("GET", path))

        if path == "/config":
            return self._send(200, CONFIG)
        if path == "/health":
            return self._send(200, {"ok": True, "assigned_shards": 0})

        if path == "/results/pending":
            with self.state.lock:
                if self.state.fail_next_pending > 0:
                    self.state.fail_next_pending -= 1
                    return self._err(503, "Service Unavailable")
            wallet, err = self._auth("GET", path, b"")
            if err:
                return self._err(401, err)
            with self.state.lock:
                if self.state.validators and wallet not in self.state.validators:
                    return self._err(403, "Forbidden: not a registered validator")
                rows = [r for rid, r in sorted(self.state.pending.items())
                        if rid not in self.state.unscorable][:50]
            return self._send(200, {"pending": rows})

        return self._err(404, "Not found")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        with self.state.lock:
            self.state.request_log.append(("POST", path))

        body = self._read_body()
        if body is None:
            return

        if path != "/verdicts":
            return self._err(404, "Not found")

        with self.state.lock:
            if self.state.fail_next_verdict > 0:
                self.state.fail_next_verdict -= 1
                return self._err(503, "Service Unavailable")

        wallet, err = self._auth("POST", path, body)
        if err:
            return self._err(401, err)
        with self.state.lock:
            if self.state.validators and wallet not in self.state.validators:
                return self._err(403, "Forbidden: not a registered validator")

        try:
            payload = json.loads(body or b"{}")
        except Exception:
            return self._err(400, "Invalid JSON")

        rid = payload.get("result_id")
        if not isinstance(rid, int):
            return self._err(400, "Missing or wrong-typed fields: result_id, validator_score")

        with self.state.lock:
            row = self.state.pending.get(rid)
        if row is None:
            return self._err(404, "Result not found")
        if rid in self.state.unscorable:
            return self._err(410, "Result is unscorable: max attempts reached")

        # ── error path (§2.5 B) ────────────────────────────────────────────
        if payload.get("status") == "validator_error":
            reason = payload.get("reason_code") or "UNKNOWN"
            with self.state.lock:
                row["scoring_attempts"] = int(row.get("scoring_attempts", 0)) + 1
                attempts = row["scoring_attempts"]
                self.state.errors.append({"result_id": rid, "reason_code": reason,
                                          "validator_wallet": wallet,
                                          "attempts": attempts})
                if attempts >= CONFIG["scoring"]["max_attempts"]:
                    self.state.unscorable.add(rid)
                    return self._send(200, {"result_id": rid, "status": "unscorable",
                                            "attempts": attempts})
                row["scoring_status"] = "pending"
                row["last_scorer_wallet"] = wallet
            return self._send(200, {"result_id": rid, "status": "pending",
                                    "attempts": attempts, "reason_code": reason})

        # ── normal path (§3.2) ─────────────────────────────────────────────
        vs = payload.get("validator_score")
        if not isinstance(vs, (int, float)) or isinstance(vs, bool):
            return self._err(400, "Missing or wrong-typed fields: result_id, validator_score")
        vs = float(vs)

        claimed = float(row["score"])
        if abs(claimed) > 50:
            return self._err(422, f"CORRUPTED_SCORE: |claimed| = {abs(claimed)} > 50")

        modality = row.get("modality", "protein")
        mt = CONFIG["modality_tolerance"][modality]
        if not (mt["sanity_min"] <= vs <= mt["sanity_max"]):
            with self.state.lock:
                row["scoring_attempts"] = int(row.get("scoring_attempts", 0)) + 1
                attempts = row["scoring_attempts"]
                self.state.errors.append({"result_id": rid,
                                          "reason_code": "RESCORE_OUT_OF_RANGE",
                                          "validator_wallet": wallet,
                                          "attempts": attempts})
                if attempts >= CONFIG["scoring"]["max_attempts"]:
                    self.state.unscorable.add(rid)
                    return self._send(200, {"result_id": rid, "status": "unscorable",
                                            "attempts": attempts})
            return self._send(200, {"result_id": rid, "status": "pending",
                                    "attempts": attempts,
                                    "reason_code": "RESCORE_OUT_OF_RANGE"})

        bias_active = bool(modality == "protein"
                           and payload.get("gpu_model")
                           and self.state.bias_factor is not None)
        bias_factor = float(self.state.bias_factor) if (bias_active and self.state.bias_factor is not None) else 1.0
        adjusted = claimed * bias_factor
        tolerance = mt["tolerance_with_bias" if bias_active else "tolerance_without_bias"]

        rel_err = abs(vs - adjusted) / abs(adjusted) if adjusted else abs(vs)
        decision = "confirm" if rel_err <= tolerance else "reject"

        hit_status = None
        if decision == "confirm":
            threshold = CONFIG["target_hit_thresholds"].get(row.get("target"))
            if threshold is None:
                hit_status = "confirmed_hit"
            else:
                hit_status = "confirmed_hit" if vs <= threshold else "confirmed_not_hit"

        vv = payload.get("validator_verdict")
        disagree = bool(vv) and vv != decision

        record = {"result_id": rid, "validator_wallet": wallet,
                  "validator_score": vs, "coord_decision": decision,
                  "hit_status": hit_status, "disagree": disagree,
                  "rel_err": round(rel_err, 6), "tolerance_used": tolerance,
                  "config_version": CONFIG["version"],
                  "bias_active": bool(bias_active), "bias_factor": bias_factor,
                  "input_hashes": payload.get("input_hashes")}
        with self.state.lock:
            self.state.verdicts.append(record)
            row["scoring_status"] = "scored"
        return self._send(200, {k: record[k] for k in
                                ("result_id", "coord_decision", "hit_status",
                                 "disagree", "rel_err", "tolerance_used",
                                 "config_version", "bias_active", "bias_factor")})


class MockCoordinator:
    """Context-managed mock server on an ephemeral localhost port."""

    def __init__(self, validators: set[str] | None = None):
        self.state = MockState(validators)
        handler = type("_Bound", (_Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "MockCoordinator":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
