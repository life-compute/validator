"""Mock payout coordinator — serves the C5b bundle fixtures over HTTP.

Routes (API_CONTRACT.md §2.11 and §2.16):

    GET /log/{YYYY-MM-DD}             -> log_get_response.json  (hash manifest)
    GET /log/{YYYY-MM-DD}/canonical   -> canonical_log.ndjson   (raw bytes)

Fixtures are read from /root/api/c5b-bundle/fixtures.  Nothing is generated:
the canonical log is served as the exact bytes on disk so its SHA-256 matches
the published log_hash and the anchored hash fixture.

GET /feed is gone: wallet age now comes from the canonical log's
wallet_first_seen events (BATCH_SPEC §13.1), not from a feed timestamp.

Fault injection: `offline` makes every route fail (503), `fail_paths` fails
one route only, and `override_canonical` serves different bytes for a day so
the log-hash / anchor-mismatch paths can be exercised.  No real coordinator,
no chain, no PM2 process is contacted.

C5b4: `hide_day(d)` removes a day from the archive entirely (both routes 404),
which is how the §6a "missing day in the replay range" refusal is tested.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUNDLE = Path("/root/api/c5b-bundle")
FIXTURES = BUNDLE / "fixtures"
TAMPERED = FIXTURES / "tampered"
TARGETS_TABLE = BUNDLE / "targets_table.json"

VALID_DAYS = (20000, 20001, 20002)
# The bundle's own open-day fixture (§13.6): closed:false, canonical -> 409.
OPEN_DAY = 20003


def day_dir(day: int) -> Path:
    if int(day) == OPEN_DAY:
        return FIXTURES / f"day_{OPEN_DAY}_open"
    return FIXTURES / f"day_{day}"


def load_batch(day: int) -> dict:
    return json.loads((day_dir(day) / "batch.json").read_text())


def load_expected_verdict(day: int) -> dict:
    return json.loads((day_dir(day) / "expected_verdict.json").read_text())


def tampered_names() -> list[str]:
    """Every tampered variant in the bundle, in file order."""
    return sorted(p.name[:-5] for p in TAMPERED.glob("*.json")
                  if not p.name.endswith(".verdict.json"))


def load_tampered(name: str) -> dict:
    return json.loads((TAMPERED / f"{name}.json").read_text())


def load_tampered_verdict(name: str) -> dict:
    return json.loads((TAMPERED / f"{name}.verdict.json").read_text())


def anchored_hashes(days=VALID_DAYS) -> dict[int, str]:
    """The MOCK of the on-chain anchored hashes: day -> logHash.

    Read from each day's anchored_log_hash.txt fixture.  This is the only
    stand-in for `anchor_hash`; no chain is contacted anywhere in the suite.
    """
    return {d: (day_dir(d) / "anchored_log_hash.txt").read_text().strip()
            for d in days}


def load_payout_envelope(day: int) -> dict:
    """The bundle's `GET /payout/{day}` response fixture (§C5r.4 envelope)."""
    return json.loads((day_dir(day) / "payout" / "batch.json").read_text())


def load_online_signature_bytes(day: int) -> bytes:
    """The bundle's sample online signature file, EXACT bytes."""
    return (day_dir(day) / "payout" / "online_signature.json").read_bytes()



def iso_for_day(day: int) -> str:
    return json.loads((day_dir(day) / "log_get_response.json").read_text())["day"]


class _State:
    def __init__(self, days):
        self.days = list(days)
        self.offline = False
        self.fail_paths: set[str] = set()
        self.request_log: list[str] = []
        self.iso_to_day = {iso_for_day(d): d for d in self.days}
        self.override_canonical: dict[int, bytes] = {}
        self.override_manifest: dict[int, dict] = {}
        # Days the archive refuses to serve at all (§6a replay gap).
        self.hidden_days: set[int] = set()
        # Days that are still OPEN (§13.6): /canonical -> 409, manifest ->
        # closed:false.  A closed day is served normally.  The bundle's own
        # open-day fixture is open by definition.
        self.open_days: set[int] = {OPEN_DAY} & set(self.days)
        # ── C5p payout routes (§8.2 / §C5r) ────────────────────────────────
        # day -> the GET /payout/{day} envelope actually served.  Defaults to
        # the bundle's payout/batch.json fixture.
        self.override_payout: dict[int, dict] = {}
        # Days with no batch built yet -> 404.
        self.no_payout_days: set[int] = set()
        # Every POST /payout/{day}/signature received, as EXACT bytes.
        self.posted_signatures: list[tuple[str, bytes]] = []
        self.signature_status: int = 200


class _Handler(BaseHTTPRequestHandler):
    state: _State = None  # type: ignore[assignment]

    def log_message(self, *a):  # silence stdlib access log
        pass

    def _send_bytes(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj) -> None:
        self._send_bytes(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        self.state.request_log.append(path)

        if self.state.offline or path in self.state.fail_paths:
            return self._send_json(503, {"error": "Service Unavailable"})

        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "log":
            day = self.state.iso_to_day.get(parts[1])
            if day is None:
                return self._send_json(404, {"error": "day not archived"})
            if day in self.state.hidden_days:
                return self._send_json(404, {"error": "day not archived"})
            if len(parts) == 2:
                manifest = self.state.override_manifest.get(day)
                if manifest is None:
                    manifest = json.loads(
                        (day_dir(day) / "log_get_response.json").read_text())
                if day in self.state.open_days:
                    # §13.6: an open day reports closed:false, no-store, and
                    # publishes no final hash.
                    manifest = {k: v for k, v in manifest.items()
                                if k not in ("log_hash", "results", "verdicts")}
                    manifest["closed"] = False
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    body = json.dumps(manifest).encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                return self._send_json(200, manifest)
            if len(parts) == 3 and parts[2] == "canonical":
                if day in self.state.open_days:
                    # §2.16: 409 Conflict + Cache-Control: no-store.
                    body = json.dumps({"error": "Day is not yet closed",
                                       "day": parts[1],
                                       "closed": False}).encode()
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                raw = self.state.override_canonical.get(day)
                if raw is None:
                    raw = (day_dir(day) / "canonical_log.ndjson").read_bytes()
                return self._send_bytes(200, raw, "application/x-ndjson")

        # ── GET /payout/{YYYY-MM-DD} (§8.2) ────────────────────────────────
        if len(parts) == 2 and parts[0] == "payout":
            day = self.state.iso_to_day.get(parts[1])
            if day is None or day in self.state.no_payout_days:
                return self._send_json(404, {"error": "no batch for that day"})
            env = self.state.override_payout.get(day)
            if env is None:
                env = load_payout_envelope(day)
            body = json.dumps(env).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            etag = (env.get("chunks") or [{}])[0].get("batchHash", "")
            self.send_header("ETag", f'"{etag}"')
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        return self._send_json(404, {"error": "Not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        self.state.request_log.append(f"POST {path}")

        if self.state.offline or path in self.state.fail_paths:
            return self._send_json(503, {"error": "Service Unavailable"})

        parts = [p for p in path.split("/") if p]
        # POST /payout/{YYYY-MM-DD}/signature (§C5r.1)
        if len(parts) == 3 and parts[0] == "payout" and parts[2] == "signature":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            # Record the EXACT bytes received, so a test can prove the file
            # and the request body are identical.
            self.state.posted_signatures.append((path, raw))
            if self.state.signature_status != 200:
                return self._send_json(self.state.signature_status,
                                       {"error": "rejected"})
            try:
                obj = json.loads(raw)
                ids = [c["batchId"] for c in obj["chunks"]]
            except Exception:
                return self._send_json(400, {"error": "malformed body"})
            return self._send_json(200, {"accepted": ids, "rejected": []})

        return self._send_json(404, {"error": "Not found"})


class MockPayoutCoordinator:
    """Context-managed fixture server on an ephemeral localhost port."""

    def __init__(self, days=VALID_DAYS):
        self.state = _State(days)
        handler = type("_Bound", (_Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def go_offline(self) -> None:
        self.state.offline = True

    def go_online(self) -> None:
        self.state.offline = False

    def set_canonical(self, day: int, raw: bytes) -> None:
        self.state.override_canonical[int(day)] = raw

    def set_manifest(self, day: int, manifest: dict) -> None:
        self.state.override_manifest[int(day)] = manifest

    def hide_day(self, day: int) -> None:
        """Make a day vanish from the archive: both routes return 404."""
        self.state.hidden_days.add(int(day))

    def unhide_day(self, day: int) -> None:
        self.state.hidden_days.discard(int(day))

    def open_day(self, day: int) -> None:
        """Make a day OPEN (§13.6): /canonical -> 409, manifest closed:false."""
        self.state.open_days.add(int(day))

    def close_day(self, day: int) -> None:
        self.state.open_days.discard(int(day))

    # ── C5p payout routes ──────────────────────────────────────────────────
    def set_payout(self, day: int, envelope: dict) -> None:
        """Serve a different GET /payout/{day} envelope (post-fetch tamper)."""
        self.state.override_payout[int(day)] = envelope

    def drop_payout(self, day: int) -> None:
        """No batch built for that day -> 404."""
        self.state.no_payout_days.add(int(day))

    def posted(self) -> list[tuple[str, bytes]]:
        """Every (path, exact body bytes) received on the signature route."""
        return list(self.state.posted_signatures)

    def set_signature_status(self, status: int) -> None:
        self.state.signature_status = int(status)

    def __enter__(self) -> "MockPayoutCoordinator":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
