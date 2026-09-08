# OPEN ITEM — WebSocket `programSubscribe` delivers no notifications

**Status**: UNRESOLVED — open as of 2026-09-08
**Explicitly NOT fixed by**: commit `ad9649e` (2026-09-08 queue/PDA work)
**Related prior investigation**: skill `solana-validator-ops`,
`references/ws-ratelimit-and-catchup-blindspot.md`

---

## What is actually wrong

The validator subscribes to `programSubscribe` on
`wss://api.devnet.solana.com` and the subscription is **accepted**:

```
2026-09-08 18:34:51  INFO  [WS] Listener thread started
2026-09-08 18:34:51  INFO  [WS] Connected → wss://api.devnet.solana.com
2026-09-08 18:34:51  INFO  [WS] Subscription confirmed  id=2874936
```

…and then delivers essentially **no `programNotification` messages**. The
socket stays ESTABLISHED, ping/pong succeeds, so every liveness check the
daemon has says "healthy". Only the absence of notifications reveals it.

Consequence: the validator is running on **catchup-poll only**. Every piece of
work it processes arrives via `_catchup_poll_once()`
(`getProgramAccounts` sweeps), never via the WebSocket.

Direct evidence tonight: with the WS "connected" the whole time, the silence
detector — which only fires when the WS has been connected >90s AND delivered
nothing for >90s — was firing continuously, for hours, across restarts.
`_ws_last_notification_time` was effectively never advancing.

## Why tonight's commit does NOT fix it

Commit `ad9649e` fixed three things, all on the *fallback* path:

1. the silence detector's re-fire cadence (hardcoded 10s floor → 60s minimum)
2. queue de-duplication on ingress
3. skipping structurally-unwinnable votes via a ValidationRecord PDA pre-check

Those made the catchup-poll path **correct and cheap**. They did nothing to
restore WebSocket delivery. Post-fix, the daemon is a healthy
polling-only validator — not a healthy event-driven one.

This distinction matters operationally: the current design tolerates a dead
WS, so the failure is now *silent and comfortable*, which makes it easier to
forget. It should not be assumed fixed because throughput recovered.

## Impact assessment (why it's real but not currently urgent)

- **Latency**: worst-case detection of a new submission is now one sweep
  interval (60s while silent) instead of near-instant. Acceptable.
- **Cost**: each sweep is two full `getProgramAccounts` calls over ~9.4k
  accounts. At the 60s floor that is sustainable but not free, and it is the
  reason the node still sees occasional HTTP 429s (3 in the first 17 min
  post-fix, all absorbed by `_rpc()` retry).
- **Scaling risk**: sweep cost grows with total program accounts (9,408 and
  climbing). Polling-only does not scale indefinitely; at some account count
  the 60s cadence will stop being affordable and latency must be traded away.
- **Not a correctness risk today**: `fetch_pending_submissions` has no
  server-side status memcmp (that blindspot was fixed 2026-09-04), so the
  sweep sees both Pending and Validating accounts. Nothing is being missed —
  it is only slower and more expensive than designed.

## What has already been ruled out / established

From `references/ws-ratelimit-and-catchup-blindspot.md` (2026-09-04) and
tonight:

- Not a subscription-rejection problem — the server ACKs with a subscription id.
- Not a TCP/keepalive death — ping/pong works, socket stays ESTAB.
- Not the daemon dropping messages in parse — the parse path is only reached
  when a notification arrives, and `_ws_last_notification_time` shows they
  do not arrive.
- Prior diagnosis was **silent server-side throttling** of `programSubscribe`
  on the public devnet endpoint: `bytes_received` grows only a few KB over
  ~20 min (ACK + keepalives), with no notification payloads.
- The 429s on `getProgramAccounts` confirm the endpoint is rate-limiting this
  IP generally, consistent with the throttling theory.

## Suggested next steps (unvalidated — for a future session)

1. **Quantify it precisely.** Find the real Python child pid
   (`pstree -p <pm2_pid>` — pm2 wraps the daemon in bash), then
   `ss -tip | grep <pid>` and watch `bytes_received` on the wss connection
   over 10–20 min. Confirm it stays in the low-KB range while submissions are
   demonstrably being created on-chain.
2. **Isolate endpoint vs client.** Run a standalone minimal
   `programSubscribe` script (no daemon) against the same endpoint and see
   whether *it* receives notifications. This cleanly separates "our WS client
   is broken" from "this endpoint won't deliver to us".
3. **Test a different RPC provider.** Point `SOLANA_WS` at a dedicated
   provider (Helius/Triton/QuickNode devnet) and check whether notifications
   flow. If they do, the fix is infrastructure (paid/dedicated endpoint), not
   code — and that also relieves the `getProgramAccounts` 429s.
4. **Consider `accountSubscribe` fan-out** as a middle path if
   `programSubscribe` specifically is what's throttled.
5. **If polling remains the design**, make it explicit rather than incidental:
   add a startup log line and a `stats.json` field recording
   `ws_notifications_received` so a dead WS is visible on the dashboard
   instead of inferred from log archaeology.

## Do not regress

- Do **not** "fix" WS silence by shortening the sweep interval again. That is
  precisely the bug fixed in `ad9649e`: it self-inflicts 429s and floods the
  work queue. `WS_SILENCE_CATCHUP_INTERVAL = 60` is a floor, not a target.
- Do **not** remove the silence detector. While the WS is dead it is the only
  thing keeping work flowing.
