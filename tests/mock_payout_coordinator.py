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


def day_dir(day: int) -> Path:
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
                return self._send_json(200, manifest)
            if len(parts) == 3 and parts[2] == "canonical":
                raw = self.state.override_canonical.get(day)
                if raw is None:
                    raw = (day_dir(day) / "canonical_log.ndjson").read_bytes()
                return self._send_bytes(200, raw, "application/x-ndjson")

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

    def __enter__(self) -> "MockPayoutCoordinator":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
