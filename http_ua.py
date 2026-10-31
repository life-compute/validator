"""The one outbound User-Agent for this repository.

Every HTTP request this repo makes — signed coordinator calls, the Solana RPC
POST, the targets fetch, and the payout CLI's fetch and post path — MUST carry
this header.

WHY THIS EXISTS
---------------
The coordinator sits behind Cloudflare.  Python's urllib sends
``User-Agent: Python-urllib/3.10`` by default, and the edge rejects that
fingerprint with ``HTTP 403 {"error": "error code: 1010"}`` BEFORE the Ed25519
X-Auth header is ever evaluated.  The symptom looks like an auth failure and is
not one: the same signed request with an honest product token returns 200.

HONESTY RULE
------------
This is a product token that identifies the client truthfully and points at a
contact URL, per RFC 9110 §10.1.5.  Do NOT replace it with a browser
User-Agent: impersonating a browser to get past an operator's edge rules is
dishonest, brittle, and hides us from the operator's own traffic analytics.  If
this token is ever blocked, that is a conversation to have with the coordinator
operator (wallet allowlisting at the edge), not something to route around.

This module deliberately imports nothing but ``os`` so that the ``payout``
package can use it without taking on any validator dependency.
"""
from __future__ import annotations

import os

#: Default product token.  Override with COORDINATOR_USER_AGENT only for a
#: deliberate, operator-coordinated change.
DEFAULT_USER_AGENT = "life-compute-validator/2.0 (+https://lifecompute.ai)"

USER_AGENT = os.environ.get("COORDINATOR_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def ua_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return *extra* with the product-token User-Agent guaranteed present.

    Never silently overwrites a caller-supplied User-Agent; callers in this
    repo are not expected to set one.
    """
    headers = dict(extra or {})
    headers.setdefault("User-Agent", USER_AGENT)
    return headers
