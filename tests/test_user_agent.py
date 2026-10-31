"""Every outbound HTTP request this repo makes MUST carry the product-token UA.

WHY: the coordinator sits behind Cloudflare, which rejects the default
``Python-urllib/3.10`` User-Agent with 403 "error code: 1010" BEFORE the
Ed25519 X-Auth header is evaluated.  That failure looks like broken auth and
is not.  These tests are the regression guard.

They intercept the transport instead of hitting the network, so they are
offline and deterministic.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from http_ua import DEFAULT_USER_AGENT, USER_AGENT, ua_headers  # noqa: E402
from coordinator_client.client import CoordinatorClient  # noqa: E402
from coordinator_client.signing import Signer  # noqa: E402
from payout.routes import PayoutRoutes  # noqa: E402
from payout.sources import LogSource  # noqa: E402


# ── the token itself ─────────────────────────────────────────────────────────

def test_user_agent_is_an_honest_product_token():
    """Must identify the product truthfully — never impersonate a browser."""
    assert USER_AGENT == DEFAULT_USER_AGENT
    assert USER_AGENT.startswith("life-compute-validator/")
    assert "+https://" in USER_AGENT, "product token should carry a contact URL"

    lowered = USER_AGENT.lower()
    for forbidden in ("mozilla", "chrome", "safari", "applewebkit",
                      "gecko", "edg/", "opera"):
        assert forbidden not in lowered, (
            f"User-Agent must not impersonate a browser (found {forbidden!r}). "
            "Spoofing a browser to pass an operator's edge rules is dishonest."
        )


def test_default_python_urllib_token_is_never_used():
    assert "python-urllib" not in USER_AGENT.lower()


def test_ua_headers_injects_and_preserves():
    assert ua_headers()["User-Agent"] == USER_AGENT
    merged = ua_headers({"Content-Type": "application/json"})
    assert merged["Content-Type"] == "application/json"
    assert merged["User-Agent"] == USER_AGENT
    # never clobbers an explicit caller value
    assert ua_headers({"User-Agent": "custom/1.0"})["User-Agent"] == "custom/1.0"


# ── coordinator client ───────────────────────────────────────────────────────

class _Capture:
    """Stand-in for urllib.request.urlopen that records the Request."""

    def __init__(self, payload: dict | None = None, status: int = 200):
        self.requests: list[urllib.request.Request] = []
        self._payload = payload if payload is not None else {"pending": []}
        self._status = status

    def __call__(self, req, timeout=None):  # noqa: ARG002
        self.requests.append(req)
        return self

    # context-manager protocol
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()

    @property
    def status(self):
        return self._status


def _ua_of(req: urllib.request.Request) -> str | None:
    # urllib normalises header names to capitalised form in .headers
    for key, value in req.header_items():
        if key.lower() == "user-agent":
            return value
    return None


@pytest.fixture
def signer():
    return Signer.generate("test-worker")


def test_coordinator_signed_get_carries_user_agent(signer):
    cap = _Capture({"pending": []})
    client = CoordinatorClient("https://coordinator.invalid", signer, opener=cap)
    assert client.get_pending() == []
    assert len(cap.requests) == 1
    assert _ua_of(cap.requests[0]) == USER_AGENT


def test_coordinator_unsigned_get_carries_user_agent(signer):
    """/config is unsigned — the WAF still sees it, so it still needs the UA."""
    cap = _Capture({"version": "2.0.0"})
    client = CoordinatorClient("https://coordinator.invalid", signer, opener=cap)
    client.get_config()
    assert _ua_of(cap.requests[0]) == USER_AGENT


def test_coordinator_post_verdict_carries_user_agent(signer):
    cap = _Capture({"ok": True})
    client = CoordinatorClient("https://coordinator.invalid", signer, opener=cap)
    client.post_verdict({"result_id": 1, "score": -8.0})
    assert _ua_of(cap.requests[0]) == USER_AGENT
    assert cap.requests[0].get_method() == "POST"


def test_coordinator_requests_still_carry_auth_and_content_type(signer):
    """The UA must be additive — it must not displace X-Auth."""
    cap = _Capture({"pending": []})
    client = CoordinatorClient("https://coordinator.invalid", signer, opener=cap)
    client.get_pending()
    headers = {k.lower(): v for k, v in cap.requests[0].header_items()}
    assert "x-auth" in headers
    assert headers["content-type"] == "application/json"
    assert headers["user-agent"] == USER_AGENT


# ── payout CLI transport (fetch + post) ──────────────────────────────────────

def test_payout_envelope_get_carries_user_agent(monkeypatch):
    cap = _Capture({"chunks": [{"entries": []}]})
    monkeypatch.setattr(urllib.request, "urlopen", cap)
    PayoutRoutes("https://coordinator.invalid").payout_envelope(20_001)
    assert len(cap.requests) == 1
    assert _ua_of(cap.requests[0]) == USER_AGENT


def test_payout_post_signature_carries_user_agent(monkeypatch):
    cap = _Capture({"accepted": True})
    monkeypatch.setattr(urllib.request, "urlopen", cap)
    PayoutRoutes("https://coordinator.invalid").post_signature(20_001, b"{}")
    assert len(cap.requests) == 1
    req = cap.requests[0]
    assert _ua_of(req) == USER_AGENT
    assert req.get_method() == "POST"


def test_payout_archive_get_carries_user_agent(monkeypatch):
    cap = _Capture({"closed": True})
    monkeypatch.setattr(urllib.request, "urlopen", cap)
    LogSource("https://coordinator.invalid")._get("/log/2026-10-31")
    assert len(cap.requests) == 1
    assert _ua_of(cap.requests[0]) == USER_AGENT


# ── daemon transport (RPC POST + targets fetch) ──────────────────────────────

def test_daemon_rpc_and_targets_carry_user_agent(monkeypatch):
    import validator_daemon as vd

    cap = _Capture({"result": "ok"})
    monkeypatch.setattr(urllib.request, "urlopen", cap)
    vd.fetch_targets()
    assert len(cap.requests) == 1, "fetch_targets must issue a Request object"
    assert _ua_of(cap.requests[0]) == USER_AGENT


# ── repo-wide guard ──────────────────────────────────────────────────────────

def test_no_production_urlopen_bypasses_the_ua_helper():
    """A bare urlopen(url_string) sends the default UA and would be blocked.

    Every production urlopen call must be handed a Request built with
    ua_headers(), never a raw URL string.
    """
    offenders: list[str] = []
    skip_dirs = {".git", ".venv-test", "tests", "node_modules", "dist",
                 "__pycache__", ".pytest_cache"}

    for py in REPO_ROOT.rglob("*.py"):
        if any(part in skip_dirs for part in py.parts):
            continue
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "urlopen(" not in stripped:
                continue
            # urlopen must receive a Request variable, not a URL literal.
            if "urlopen(url" in stripped or "urlopen(f\"" in stripped \
                    or "urlopen('" in stripped or "urlopen(\"" in stripped \
                    or "urlopen(TARGETS_URL" in stripped \
                    or "urlopen(SOLANA_RPC" in stripped:
                offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {stripped}")

    assert not offenders, (
        "these outbound calls pass a URL straight to urlopen and would send "
        "the default Python-urllib User-Agent (Cloudflare 1010):\n  "
        + "\n  ".join(offenders)
    )
