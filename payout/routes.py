"""HTTP client for the C5o payout routes (API_CONTRACT §8.2, §C5r).

Two routes are used by the checker:

    GET  /payout/{YYYY-MM-DD}             -> the unsigned batch envelope
    POST /payout/{YYYY-MM-DD}/signature   -> submit the signature file

The GET response is an ENVELOPE (§C5r.4):

    { "day": "YYYY-MM-DD", "chunks": [ { ...BatchFile, batchState, entryCount } ] }

`batchState` and `entryCount` are DB-level fields that are NOT part of the
canonical BatchFile and are NOT hashed (§4).  `chunks_from_envelope` returns
the chunk objects exactly as served — the verifier recomputes the hash from
the entries itself, so a doctored envelope cannot smuggle a hash through.

Fail closed: any transport, status or shape problem raises
CoordinatorUnreachable, which the caller turns into "no signature".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .batch_hash import day_to_iso
from .errors import CoordinatorUnreachable

import sys as _sys
from pathlib import Path as _Path

_repo_root = str(_Path(__file__).resolve().parent.parent)
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)
from http_ua import ua_headers  # noqa: E402


class PayoutRoutes:
    """Reader/writer for the payout routes.  No auth: both are public."""

    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── GET /payout/{day} ───────────────────────────────────────────────────
    def payout_envelope(self, day: int) -> dict[str, Any]:
        path = f"/payout/{day_to_iso(day)}"
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url, headers=ua_headers(), method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    raise CoordinatorUnreachable(
                        f"GET {path} returned HTTP {resp.status}")
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise CoordinatorUnreachable(
                f"GET {path} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            # Never echo the URL: it can carry credentials.
            raise CoordinatorUnreachable(
                f"GET {path} failed: {type(exc).__name__}") from None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise CoordinatorUnreachable(
                f"GET {path} returned non-JSON body") from None
        if not isinstance(obj, dict):
            raise CoordinatorUnreachable(f"GET {path} returned a non-object")
        return obj

    @staticmethod
    def chunks_from_envelope(envelope: dict[str, Any]) -> list[dict[str, Any]]:
        """§C5r.4: take `chunks[]` out of the HTTP envelope, unmodified."""
        chunks = envelope.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise CoordinatorUnreachable(
                "GET /payout/{day} envelope carries no chunks[] array")
        for c in chunks:
            if not isinstance(c, dict):
                raise CoordinatorUnreachable(
                    "GET /payout/{day} chunks[] entry is not an object")
        return list(chunks)

    # ── POST /payout/{day}/signature ────────────────────────────────────────
    def post_signature(self, day: int, body: bytes) -> dict[str, Any]:
        """POST the signature file's EXACT bytes (§C5r.1)."""
        path = f"/payout/{day_to_iso(day)}/signature"
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url, data=bytes(body), method="POST",
            headers=ua_headers({"Content-Type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = f": {exc.read().decode('utf-8', 'replace')[:200]}"
            except Exception:
                pass
            raise CoordinatorUnreachable(
                f"POST {path} returned HTTP {exc.code}{detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise CoordinatorUnreachable(
                f"POST {path} failed: {type(exc).__name__}") from None
        if status != 200:
            raise CoordinatorUnreachable(f"POST {path} returned HTTP {status}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise CoordinatorUnreachable(
                f"POST {path} returned non-JSON body") from None
