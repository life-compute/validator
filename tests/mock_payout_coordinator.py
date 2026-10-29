"""Mock payout coordinator — serves the C5b bundle fixtures over HTTP.

Routes (API_CONTRACT.md §2.9, §2.11 plus the canonical-log body route the
archive serves):

    GET /log/{YYYY-MM-DD}             -> log_get_response.json  (hash manifest)
    GET /log/{YYYY-MM-DD}/canonical   -> canonical_log.ndjson   (raw bytes)
    GET /feed                         -> feed_get_response.json

Fixtures are read from /root/api/c5b-bundle/fixtures.  Nothing is generated:
the canonical log is served as the exact bytes on disk so its SHA-256 matches
the anchored hash fixture.

Fault injection: `offline` makes every route fail (connection refused is
simulated by a 503), `fail_paths` fails one route only.  No real coordinator,
no chain, no PM2 process is contacted.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUNDLE = Path("/root/api/c5b-bundle")
FIXTURES = BUNDLE / "fixtures"


def day_dir(day: int) -> Path:
    return FIXTURES / f"day_{day}"


def load_batch(day: int) -> dict:
    return json.loads((day_dir(day) / "batch.json").read_text())


def load_expected_verdict(day: int) -> dict:
    return json.loads((day_dir(day) / "expected_verdict.json").read_text())


def load_tampered(name: str) -> dict:
    return json.loads((FIXTURES / "tampered" / f"{name}.json").read_text())


def load_tampered_verdict(name: str) -> dict:
    return json.loads((FIXTURES / "tampered" / f"{name}.verdict.json").read_text())


def anchored_hashes(days=(20000, 20001, 20002)) -> dict[int, str]:
    """The mock of the on-chain anchored hashes: day -> logHash."""
    return {d: (day_dir(d) / "anchored_log_hash.txt").read_text().strip()
            for d in days}


def iso_for_day(day: int) -> str:
    return json.loads((day_dir(day) / "log_get_response.json").read_text())["day"]


class _State:
    def __init__(self, days):
        self.days = list(days)
        self.offline = False
        self.fail_paths: set[str] = set()
        self.request_log: list[str] = []
        self.iso_to_day = {iso_for_day(d): d for d in self.days}


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

        if path == "/feed":
            # Union of every fixture day's feed: the real /feed is a rolling
            # window across days, not per-day.
            rows = []
            for d in self.state.days:
                obj = json.loads((day_dir(d) / "feed_get_response.json").read_text())
                rows.extend(obj.get("results", []))
            return self._send_json(200, {"results": rows})

        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "log":
            iso = parts[1]
            day = self.state.iso_to_day.get(iso)
            if day is None:
                return self._send_json(404, {"error": "day not archived"})
            if len(parts) == 2:
                return self._send_json(
                    200, json.loads((day_dir(day) / "log_get_response.json").read_text()))
            if len(parts) == 3 and parts[2] == "canonical":
                raw = (day_dir(day) / "canonical_log.ndjson").read_bytes()
                return self._send_bytes(200, raw, "application/x-ndjson")

        return self._send_json(404, {"error": "Not found"})


class MockPayoutCoordinator:
    """Context-managed fixture server on an ephemeral localhost port."""

    def __init__(self, days=(20000, 20001, 20002)):
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

    def __enter__(self) -> "MockPayoutCoordinator":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
