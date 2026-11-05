"""C44b — anchors read from the CHAIN (payout.chain_anchors.ChainAnchorBuilder).

Every positive case here runs against the FOUR REAL devnet transactions the
operator published for 2026-10-31..2026-11-03, captured read-only by
tools/fetch_anchor_fixtures.py into tests/fixtures/chain_anchors/ (shortened
to the fields the checker reads, structurally identical to what
`getTransaction jsonParsed` returns).  The negative cases are MUTATIONS of
those same real responses — another signer, a malformed memo, a failed
transaction, a wrong program id, a conflicting second anchor — so no fixture
is invented from scratch.

No network, no key, no chain is contacted by this module: the RPC is a fake
that serves the recorded JSON and counts the calls.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from payout import chain_anchors as CA  # noqa: E402
from payout.chain_anchors import (  # noqa: E402
    AnchorConflict,
    AnchorRpcUnavailable,
    ChainAnchorBuilder,
    IGNORED_FAILED_TX,
    IGNORED_MALFORMED,
    IGNORED_NOT_OPERATOR,
    IGNORED_WRONG_PROGRAM,
    SignatureCache,
    parse_anchor_memo,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chain_anchors"
OPERATOR = "CZbXxyCmUP4wdcumPXBx3w2RG2dVoWZQUrVw5YsmaNWk"
OTHER_SIGNER = "GLThbWgJQNHQpxuRVxyKystNxjMo47qhtgpe2sW4oGDq"   # the fee payer
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
PROGRAM_ID = "HhS2coMkjRc5SSPsJn4mTXcFYwWL9hz6cfAzoWKymadv"

# The four anchors as they are ON CHAIN right now (day -> log hash).
LIVE_ANCHORS = {
    20757: "60ce558188ee2abd32a62d139161b5fcabb6a31fd5f032d0acae319fd2970b25",
    20758: "f31c505a4ac189878155798ab4509fa369ddae63850ba29d7fba186d435deda7",
    20759: "126ee75bf8c8e37595392626cdd35ae0f8fe7d6d304baa7c917ea9471f197837",
    20760: "43c4ca027e6b232ff9b20e0e7517c6d5aed2e90c6c7b2a828d18b8d022c57e9c",
}


def live_signatures() -> list[dict]:
    return json.loads((FIXTURES / "signatures_page.json").read_text())


def live_transactions() -> dict[str, dict]:
    return json.loads((FIXTURES / "transactions.json").read_text())


def sig_for_day(day: int) -> str:
    """The real signature that anchors `day`."""
    for sig, tx in live_transactions().items():
        memo = tx["transaction"]["message"]["instructions"][0]["parsed"]
        if f"day={day} " in memo:
            return sig
    raise AssertionError(f"no fixture anchors day {day}")


# ── the fake RPC ────────────────────────────────────────────────────────────
class FakeRpc:
    """Serves recorded pages + transactions and COUNTS every call.

    `fail_with` raises on the Nth call (used for the 429 / RPC-error cases);
    `pages` lets a test split the signature list over several pages so the
    pagination loop is exercised exactly as it is against the live RPC.
    """

    def __init__(self, pages: list[list[dict]], txs: dict[str, dict],
                 *, fail_with: Exception | None = None, fail_on: int = 1):
        self.pages = pages
        self.txs = txs
        self.fail_with = fail_with
        self.fail_on = fail_on
        self.calls: list[tuple[str, list]] = []
        self.tx_fetches: list[str] = []

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        if self.fail_with is not None and len(self.calls) >= self.fail_on:
            # Mapped exactly as SolanaRpc.call maps it, so the fake cannot be
            # kinder to the caller than the real client is.  The mapping
            # itself is tested separately against the real SolanaRpc.
            import urllib.error
            if isinstance(self.fail_with, urllib.error.HTTPError):
                raise AnchorRpcUnavailable(
                    f"{method}: RPC answered HTTP {self.fail_with.code}")
            raise self.fail_with
        if method == "getSignaturesForAddress":
            before = (params[1] or {}).get("before")
            if before is None:
                return list(self.pages[0])
            for i, page in enumerate(self.pages):
                if any(p["signature"] == before for p in page):
                    return list(self.pages[i + 1]) if i + 1 < len(self.pages) else []
            return []
        if method == "getTransaction":
            self.tx_fetches.append(params[0])
            return self.txs.get(params[0])
        raise AssertionError(f"unexpected RPC method {method}")


def builder(rpc: FakeRpc, *, cache_path=None, program_id: str = PROGRAM_ID,
            operator: str = OPERATOR, page_size: int = 1000
            ) -> ChainAnchorBuilder:
    return ChainAnchorBuilder(program_id=program_id, operator_pubkey=operator,
                              memo_program=MEMO_PROGRAM, rpc=rpc,
                              cache_path=cache_path, page_size=page_size,
                              warn=lambda *_a, **_k: None)


def one_page_rpc(**kw) -> FakeRpc:
    return FakeRpc([live_signatures()], live_transactions(), **kw)


# ════════════════════════════════════════════════════════════════════════════
# 1. The four REAL anchors are read, accepted and attributed to the right day
# ════════════════════════════════════════════════════════════════════════════
def test_the_four_live_devnet_anchors_are_read_from_the_chain():
    scan = builder(one_page_rpc()).collect(sorted(LIVE_ANCHORS))
    assert {d: a.log_hash for d, a in scan.anchors.items()} == LIVE_ANCHORS
    assert scan.conflicts == {}
    assert scan.ignored == {}


@pytest.mark.parametrize("day,date", [(20757, "2026-10-31"), (20758, "2026-11-01"),
                                      (20759, "2026-11-02"), (20760, "2026-11-03")])
def test_each_real_anchor_carries_the_right_day_date_program_and_signature(day, date):
    scan = builder(one_page_rpc()).collect([day])
    rec = scan.anchors[day]
    assert (rec.date, rec.program_id) == (date, PROGRAM_ID)
    assert rec.log_hash == LIVE_ANCHORS[day]
    assert rec.signature == sig_for_day(day)
    assert rec.slot > 0


def test_build_returns_only_the_days_that_were_asked_for():
    assert builder(one_page_rpc()).build([20758]) == {20758: LIVE_ANCHORS[20758]}


# ════════════════════════════════════════════════════════════════════════════
# 2. The memo grammar
# ════════════════════════════════════════════════════════════════════════════
def test_the_real_memo_text_parses_exactly():
    tx = live_transactions()[sig_for_day(20757)]
    memo = tx["transaction"]["message"]["instructions"][0]["parsed"]
    parsed = parse_anchor_memo(memo)
    # C47 §17.4: the parser now reports which anchor VERSION it read, and the
    # `signed=` hash that only a /2 memo carries.  A /1 memo is version 1 with
    # no signed hash — the live day 20757 anchor is exactly that.
    assert parsed == {"day": 20757, "date": "2026-10-31",
                      "log_hash": LIVE_ANCHORS[20757], "signed_hash": None,
                      "program_id": PROGRAM_ID, "version": 1}


@pytest.mark.parametrize("memo", [
    "",
    "hello",
    "LIFE-ANCHOR/2 day=20757 date=2026-10-31 log=" + "a" * 64 + f" program={PROGRAM_ID}",
    f"LIFE-ANCHOR/1 day=20757 date=2026-10-31 log={'a' * 63} program={PROGRAM_ID}",
    f"LIFE-ANCHOR/1 day=20757 date=2026-10-31 log={'z' * 64} program={PROGRAM_ID}",
    "LIFE-ANCHOR/1 date=2026-10-31 log=" + "a" * 64 + f" program={PROGRAM_ID}",
    f"LIFE-ANCHOR/1 day=20757 date=2026-10-31 log={'a' * 64} program={PROGRAM_ID} extra",
    f"prefix LIFE-ANCHOR/1 day=20757 date=2026-10-31 log={'a' * 64} program={PROGRAM_ID}",
])
def test_a_memo_that_is_not_exactly_the_grammar_is_not_an_anchor(memo):
    assert parse_anchor_memo(memo) is None


# ════════════════════════════════════════════════════════════════════════════
# 3. Ignored transactions — counted, never an anchor, never a refusal
# ════════════════════════════════════════════════════════════════════════════
def _mutate(day: int, fn) -> FakeRpc:
    """Serve ONE real transaction, mutated by `fn`."""
    sig = sig_for_day(day)
    txs = {sig: copy.deepcopy(live_transactions()[sig])}
    fn(txs[sig])
    sigs = [s for s in live_signatures() if s["signature"] == sig]
    return FakeRpc([sigs], txs)


def test_a_memo_from_another_signer_is_ignored():
    def other_signer(tx):
        for key in tx["transaction"]["message"]["accountKeys"]:
            if key["pubkey"] == OPERATOR:
                key["pubkey"] = OTHER_SIGNER
                key["signer"] = True
    scan = builder(_mutate(20757, other_signer)).collect([20757])
    assert scan.anchors == {}
    assert scan.ignored == {IGNORED_NOT_OPERATOR: 1}


def test_the_operator_merely_being_mentioned_is_not_enough_it_must_sign():
    def not_a_signer(tx):
        for key in tx["transaction"]["message"]["accountKeys"]:
            if key["pubkey"] == OPERATOR:
                key["signer"] = False
    scan = builder(_mutate(20758, not_a_signer)).collect([20758])
    assert scan.anchors == {}
    assert scan.ignored == {IGNORED_NOT_OPERATOR: 1}


def test_a_malformed_memo_is_ignored():
    def malformed(tx):
        tx["transaction"]["message"]["instructions"][0]["parsed"] = \
            "LIFE-ANCHOR/1 day=20757 log=deadbeef"
    scan = builder(_mutate(20757, malformed)).collect([20757])
    assert scan.anchors == {}
    assert scan.ignored == {IGNORED_MALFORMED: 1}


def test_a_failed_transaction_is_ignored():
    def failed(tx):
        tx["meta"]["err"] = {"InstructionError": [0, {"Custom": 1}]}
        tx["meta"]["status"] = {"Err": {"InstructionError": [0, {"Custom": 1}]}}
    scan = builder(_mutate(20759, failed)).collect([20759])
    assert scan.anchors == {}
    assert scan.ignored == {IGNORED_FAILED_TX: 1}


def test_a_memo_for_another_program_id_is_ignored():
    scan = builder(one_page_rpc(),
                   program_id="74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf"
                   ).collect(sorted(LIVE_ANCHORS))
    assert scan.anchors == {}
    assert scan.ignored == {IGNORED_WRONG_PROGRAM: 4}


def test_an_instruction_of_another_program_is_not_a_memo():
    def wrong_program(tx):
        tx["transaction"]["message"]["instructions"][0]["programId"] = \
            "11111111111111111111111111111111"
    scan = builder(_mutate(20760, wrong_program)).collect([20760])
    assert scan.anchors == {}
    assert scan.ignored == {CA.IGNORED_NO_MEMO: 1}


def test_day_and_date_must_agree():
    def skewed(tx):
        ix = tx["transaction"]["message"]["instructions"][0]
        ix["parsed"] = ix["parsed"].replace("date=2026-10-31", "date=2026-10-30")
    scan = builder(_mutate(20757, skewed)).collect([20757])
    assert scan.anchors == {}
    assert scan.ignored == {CA.IGNORED_DAY_DATE: 1}


def test_ignored_transactions_never_block_a_good_day():
    """A junk memo next to the real anchors leaves all four anchors intact."""
    sigs = live_signatures()
    txs = live_transactions()
    junk_sig = "J" + "1" * 86
    junk = copy.deepcopy(txs[sig_for_day(20757)])
    junk["transaction"]["message"]["instructions"][0]["parsed"] = "not an anchor"
    txs = dict(txs, **{junk_sig: junk})
    sigs = [{"signature": junk_sig, "slot": 1, "err": None,
             "blockTime": 1, "confirmationStatus": "finalized",
             "memo": "[12] not an anchor"}] + sigs
    scan = builder(FakeRpc([sigs], txs)).collect(sorted(LIVE_ANCHORS))
    assert {d: a.log_hash for d, a in scan.anchors.items()} == LIVE_ANCHORS
    assert scan.ignored == {IGNORED_MALFORMED: 1}


# ════════════════════════════════════════════════════════════════════════════
# 4. anchor_missing — transient, never a refusal
# ════════════════════════════════════════════════════════════════════════════
def test_a_day_with_no_anchor_is_simply_absent_not_an_error():
    scan = builder(one_page_rpc()).collect([20761])
    assert scan.anchors == {}
    assert builder(one_page_rpc()).build([20761]) == {}


def test_a_missing_day_does_not_hide_the_days_that_do_have_anchors():
    built = builder(one_page_rpc()).build([20758, 20761, 20760])
    assert built == {20758: LIVE_ANCHORS[20758], 20760: LIVE_ANCHORS[20760]}


# ════════════════════════════════════════════════════════════════════════════
# 5. anchor_conflict — two VALID anchors, different hashes
# ════════════════════════════════════════════════════════════════════════════
def _conflicting_rpc() -> FakeRpc:
    """A second, equally valid anchor for day 20758 with a different hash."""
    real_sig = sig_for_day(20758)
    txs = live_transactions()
    dup_sig = "C" + "2" * 86
    dup = copy.deepcopy(txs[real_sig])
    ix = dup["transaction"]["message"]["instructions"][0]
    ix["parsed"] = ix["parsed"].replace(LIVE_ANCHORS[20758], "b" * 64)
    dup["slot"] = txs[real_sig]["slot"] + 10
    txs = dict(txs, **{dup_sig: dup})
    sigs = [{"signature": dup_sig, "slot": dup["slot"], "err": None,
             "blockTime": 1, "confirmationStatus": "finalized",
             "memo": ix["parsed"]}] + live_signatures()
    return FakeRpc([sigs], txs)


def test_two_valid_anchors_with_different_hashes_are_a_conflict():
    scan = builder(_conflicting_rpc()).collect([20758])
    assert 20758 in scan.conflicts
    hashes = {r.log_hash for r in scan.conflicts[20758]}
    assert hashes == {LIVE_ANCHORS[20758], "b" * 64}


def test_build_refuses_on_a_conflict_and_names_the_day():
    with pytest.raises(AnchorConflict) as exc:
        builder(_conflicting_rpc()).build([20758])
    assert "20758" in str(exc.value)


def test_the_same_anchor_published_twice_is_not_a_conflict():
    real_sig = sig_for_day(20757)
    txs = live_transactions()
    dup_sig = "D" + "3" * 86
    txs = dict(txs, **{dup_sig: copy.deepcopy(txs[real_sig])})
    sigs = [{"signature": dup_sig, "slot": 9, "err": None, "blockTime": 1,
             "confirmationStatus": "finalized", "memo": ""}] + live_signatures()
    scan = builder(FakeRpc([sigs], txs)).collect([20757])
    assert scan.conflicts == {}
    assert scan.anchors[20757].log_hash == LIVE_ANCHORS[20757]


# ════════════════════════════════════════════════════════════════════════════
# 6. Pagination
# ════════════════════════════════════════════════════════════════════════════
def test_the_scan_pages_through_the_whole_signature_history():
    sigs = live_signatures()
    assert len(sigs) == 4
    rpc = FakeRpc([sigs[:2], sigs[2:], []], live_transactions())
    scan = builder(rpc, page_size=2).collect(sorted(LIVE_ANCHORS))
    assert {d: a.log_hash for d, a in scan.anchors.items()} == LIVE_ANCHORS
    assert scan.pages >= 2
    pagings = [c for c in rpc.calls if c[0] == "getSignaturesForAddress"]
    assert len(pagings) >= 2
    assert pagings[1][1][1]["before"] == sigs[1]["signature"]


def test_a_short_page_ends_the_scan_without_another_request():
    rpc = FakeRpc([live_signatures()], live_transactions())
    builder(rpc, page_size=1000).collect(sorted(LIVE_ANCHORS))
    assert len([c for c in rpc.calls if c[0] == "getSignaturesForAddress"]) == 1


def test_every_page_asks_for_finalized_commitment():
    rpc = one_page_rpc()
    builder(rpc).collect(sorted(LIVE_ANCHORS))
    for method, params in rpc.calls:
        assert params[1]["commitment"] == "finalized", method


# ════════════════════════════════════════════════════════════════════════════
# 7. The incremental cache — a finalized transaction is fetched ONCE
# ════════════════════════════════════════════════════════════════════════════
def test_a_finalized_transaction_is_fetched_once_and_then_cached(tmp_path):
    cache = tmp_path / "anchor_signature_cache.json"
    first = one_page_rpc()
    scan1 = builder(first, cache_path=cache).collect(sorted(LIVE_ANCHORS))
    assert len(first.tx_fetches) == 4
    assert scan1.transactions_fetched == 4 and scan1.cache_hits == 0
    assert cache.is_file()

    second = one_page_rpc()
    scan2 = builder(second, cache_path=cache).collect(sorted(LIVE_ANCHORS))
    assert second.tx_fetches == []          # not one getTransaction re-issued
    assert scan2.cache_hits == 4 and scan2.transactions_fetched == 0
    assert {d: a.log_hash for d, a in scan2.anchors.items()} == LIVE_ANCHORS


def test_the_cache_also_remembers_why_a_transaction_was_ignored(tmp_path):
    cache = tmp_path / "cache.json"
    def malformed(tx):
        tx["transaction"]["message"]["instructions"][0]["parsed"] = "junk"
    builder(_mutate(20757, malformed), cache_path=cache).collect([20757])
    again = _mutate(20757, malformed)
    scan = builder(again, cache_path=cache).collect([20757])
    assert again.tx_fetches == []
    assert scan.ignored == {IGNORED_MALFORMED: 1}


def test_a_transaction_that_is_not_finalized_yet_is_not_cached(tmp_path):
    cache = tmp_path / "cache.json"
    sigs = [{"signature": "N" + "4" * 86, "slot": 1, "err": None,
             "blockTime": 1, "confirmationStatus": "confirmed", "memo": ""}]
    rpc = FakeRpc([sigs], {})               # getTransaction -> None
    scan = builder(rpc, cache_path=cache).collect([20757])
    assert scan.anchors == {}
    assert SignatureCache(cache).get(sigs[0]["signature"]) is None


def test_a_corrupt_cache_file_is_ignored_not_fatal(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text("{not json")
    scan = builder(one_page_rpc(), cache_path=cache).collect(sorted(LIVE_ANCHORS))
    assert {d: a.log_hash for d, a in scan.anchors.items()} == LIVE_ANCHORS


# ════════════════════════════════════════════════════════════════════════════
# 8. RPC failures are TRANSIENT — 429, 5xx, JSON-RPC error, transport
# ════════════════════════════════════════════════════════════════════════════
def test_a_429_rate_limit_is_transient_and_never_a_refusal():
    import urllib.error
    rpc = one_page_rpc(fail_with=urllib.error.HTTPError(
        "http://rpc", 429, "Too Many Requests", {}, None))
    with pytest.raises(AnchorRpcUnavailable) as exc:
        builder(rpc).collect([20758])
    assert not isinstance(exc.value, AnchorConflict)


@pytest.mark.parametrize("code", [429, 500, 503])
def test_every_http_error_from_the_rpc_is_transient(code):
    import urllib.error
    rpc = one_page_rpc(fail_with=urllib.error.HTTPError(
        "http://rpc", code, "boom", {}, None))
    with pytest.raises(AnchorRpcUnavailable):
        builder(rpc).collect([20758])


def test_a_rate_limit_part_way_through_does_not_half_accept_a_day():
    """Failing on the first getTransaction must not leave a partial map."""
    import urllib.error
    rpc = one_page_rpc(fail_with=urllib.error.HTTPError(
        "http://rpc", 429, "Too Many Requests", {}, None), fail_on=2)
    with pytest.raises(AnchorRpcUnavailable):
        builder(rpc).collect(sorted(LIVE_ANCHORS))


def test_a_json_rpc_error_object_is_transient():
    class ErrRpc(FakeRpc):
        def call(self, method, params):
            raise AnchorRpcUnavailable(f"{method}: RPC error -32005: rate limit")
    with pytest.raises(AnchorRpcUnavailable):
        builder(ErrRpc([live_signatures()], live_transactions())).collect([20758])


def test_the_real_rpc_client_turns_a_429_into_the_transient_error(monkeypatch):
    """The SolanaRpc client itself, not just the fake, must map 429 -> transient."""
    import urllib.error
    import urllib.request

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(AnchorRpcUnavailable) as exc:
        CA.SolanaRpc("https://api.devnet.solana.com").call("getSlot", [])
    assert "429" in str(exc.value)
    assert "api.devnet.solana.com" not in str(exc.value)   # no URL echo


def test_the_real_rpc_client_reads_the_recorded_devnet_responses(monkeypatch):
    """End to end through the PRODUCTION client over the recorded bytes.

    Only `urlopen` is replaced; SolanaRpc's own request building, JSON-RPC
    envelope handling and the builder's parsing all run for real, against the
    exact responses devnet returned for the four live anchors.
    """
    import io
    import urllib.request

    sigs = live_signatures()
    txs = live_transactions()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        call = json.loads(req.data)
        if call["method"] == "getSignaturesForAddress":
            result = sigs if call["params"][1].get("before") is None else []
        else:
            result = txs.get(call["params"][0])
        return _Resp(json.dumps(
            {"jsonrpc": "2.0", "id": call["id"], "result": result}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    b = ChainAnchorBuilder(program_id=PROGRAM_ID, operator_pubkey=OPERATOR,
                           memo_program=MEMO_PROGRAM, warn=lambda *_a, **_k: None)
    assert b.build(sorted(LIVE_ANCHORS)) == LIVE_ANCHORS


# ════════════════════════════════════════════════════════════════════════════
# 9. Configuration
# ════════════════════════════════════════════════════════════════════════════
def test_the_operator_pubkey_is_required():
    from payout.errors import PayoutError
    with pytest.raises(PayoutError):
        ChainAnchorBuilder(program_id=PROGRAM_ID, operator_pubkey="")


def test_the_defaults_are_devnet_and_the_spl_memo_program():
    assert CA.DEFAULT_ANCHOR_RPC_URL == "https://api.devnet.solana.com"
    assert CA.DEFAULT_MEMO_PROGRAM == MEMO_PROGRAM


def test_the_module_reads_no_wall_clock():
    """Same guard as the rest of the package (BATCH_SPEC §2 / C5j)."""
    import tokenize
    banned = ("NOW_MS", "now_ms", "time.time", "time_ns", "datetime.now",
              "utcnow", "date.today", "monotonic", "perf_counter")
    src = Path(CA.__file__)
    toks = []
    with src.open("rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            toks.append(tok.string)
    code = "".join(toks).replace(".", "")
    assert not [b for b in banned if b.replace(".", "") in code]
