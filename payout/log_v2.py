"""Log Format V2 (BATCH_SPEC §17 / CHECKER_CHANGE_REQUEST_C47) — the checker.

Activated per day by LOG_V2_FROM_DAY.  Below that day nothing here runs and
the v1 behaviour is untouched; at or above it, a day is additionally checked
line by line against the signed request log.

THE LOGICAL RECORD.  canonical.ndjson keeps its six v1 fields and its hash
(§17.3), so the v2 record is ASSEMBLED, never read whole:

    result_id, wallet, target, molecule_hash, archived_at, coord_score
                                        ← canonical.ndjson (hashed as `log=`)
    validator_wallet, miner_req, verdict_req
                                        ← signed.ndjson   (hashed as `signed=`)
    validator_score                     ← decoded from verdict.body_b64
    coord_decision, hit_status          ← RECOMPUTED from coord_score,
                                          bias_factor and the pinned
                                          scoring_config.json
    config_version, bias_factor         ← §17.3: not in either committed file.
                                          A checker with no DB access uses the
                                          config's `coord_config_version` and
                                          bias_factor = 1.0.

RULE ORDER IS NOT OURS TO CHOOSE.  `evaluate_day` runs exactly the order the
reference verifier (spec/golden/log_v2/verify.ts) runs, which is also the
order the golden README lists:

    anchor_v2_mismatch        (canonical hash, signed hash, memo fields,
                               log=, signed=, then day/date consistency)
    scoring_config_unknown
    per signed line, in file order:
        validator_self_verify
        wallet_not_registered     (miner, then validator)
        duplicate_signed_request  (nonce ledger)
        timestamp_out_of_range
        miner_sig_invalid
        validator_sig_invalid
        score_body_mismatch
        mol_hash_body_mismatch
        result_id_body_mismatch
        target_body_mismatch
        miner_wallet_body_mismatch
        decision_mismatch
        hit_mismatch
    validator_over_limit
    validator_pay_mismatch

The FIRST rule that fires is the answer; refusal codes are lower_snake_case
(§17.6b).

WHAT `decision_mismatch` COMPARES AGAINST.  The canonical line carries no
coord_decision (it is a v1 line), so the recomputed decision is compared
with the only signed claim that exists: `validator_verdict` in the verdict
body, which is inside the Ed25519-signed material.  For the golden vectors
the manifest's per-validator `coord_decision` / `hit_status` are supplied as
explicit expectations instead, exactly as verify.ts does.  `hit_status` has
no signed counterpart at all, so outside the golden path it is recomputed
and reported, never compared against an unsigned value.

NO WALL CLOCK.  Like every module in this package: timestamps are compared
against `archived_at`, which the log itself carries.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .batch_hash import day_to_iso
from .scoring_config import DEFAULT_MODALITY, ScoringConfig

# ── refusal codes (§17.6b: lower_snake_case) ────────────────────────────────
MINER_SIG_INVALID = "miner_sig_invalid"
VALIDATOR_SIG_INVALID = "validator_sig_invalid"
SIGNED_DIGEST_MISMATCH = "signed_digest_mismatch"
SCORE_BODY_MISMATCH = "score_body_mismatch"
MOL_HASH_BODY_MISMATCH = "mol_hash_body_mismatch"
RESULT_ID_BODY_MISMATCH = "result_id_body_mismatch"
TARGET_BODY_MISMATCH = "target_body_mismatch"
MINER_WALLET_BODY_MISMATCH = "miner_wallet_body_mismatch"
VALIDATOR_SELF_VERIFY = "validator_self_verify"
DECISION_MISMATCH = "decision_mismatch"
HIT_MISMATCH = "hit_mismatch"
TIMESTAMP_OUT_OF_RANGE = "timestamp_out_of_range"
DUPLICATE_SIGNED_REQUEST = "duplicate_signed_request"
WALLET_NOT_REGISTERED = "wallet_not_registered"
ANCHOR_V2_MISMATCH = "anchor_v2_mismatch"
VALIDATOR_PAY_MISMATCH = "validator_pay_mismatch"
VALIDATOR_OVER_LIMIT = "validator_over_limit"
SCORING_CONFIG_UNKNOWN = "scoring_config_unknown"

ACCEPTED = "accepted"

# §6.3-new default, also the §17 golden vector's K.
VALIDATORS_PER_RESULT = 2

# §17.2 timestamp windows, in SECONDS against archived_at/1000.
MINER_TS_BACK_S = 172_800        # 48 h
MINER_TS_FORWARD_S = 300         # 5 min
VERDICT_TS_WINDOW_S = 600        # ±10 min


# ── parsed inputs ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SignedRequest:
    wallet: str
    method: str
    path: str
    body_b64: str
    timestamp: int
    nonce: str
    signature: str

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "SignedRequest":
        missing = [k for k in ("wallet", "method", "path", "body_b64",
                               "timestamp", "nonce", "signature")
                   if k not in obj]
        if missing:
            raise ValueError(f"signed request missing {missing}")
        return cls(wallet=str(obj["wallet"]), method=str(obj["method"]),
                   path=str(obj["path"]), body_b64=str(obj["body_b64"]),
                   timestamp=int(obj["timestamp"]), nonce=str(obj["nonce"]),
                   signature=str(obj["signature"]))

    def body_bytes(self) -> bytes:
        return base64.b64decode(self.body_b64)

    def body_json(self) -> dict[str, Any]:
        obj = json.loads(self.body_bytes().decode("utf-8"))
        return obj if isinstance(obj, dict) else {}

    def signed_message(self) -> str:
        """METHOD\\nPATH\\nsha256hex(body)\\nnonce\\ntimestamp (contract §1)."""
        body_hash = hashlib.sha256(self.body_bytes()).hexdigest()
        return (f"{self.method}\n{self.path}\n{body_hash}\n"
                f"{self.nonce}\n{self.timestamp}")

    def digest(self) -> str:
        """§17.3 signed-request digest: sha256(signed_msg + "\\n" + sig_hex)."""
        return hashlib.sha256(
            (self.signed_message() + "\n" + self.signature).encode("utf-8")
        ).hexdigest()

    def as_json_obj(self) -> dict[str, Any]:
        return {"wallet": self.wallet, "method": self.method,
                "path": self.path, "body_b64": self.body_b64,
                "timestamp": self.timestamp, "nonce": self.nonce,
                "signature": self.signature}


@dataclass(frozen=True)
class SignedLine:
    result_id: int
    validator_wallet: str
    miner: SignedRequest
    verdict: SignedRequest
    test_override_hit_status: str | None = None
    test_override_config_version: str | None = None

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "SignedLine":
        for k in ("result_id", "validator_wallet", "miner", "verdict"):
            if k not in obj:
                raise ValueError(f"signed.ndjson line missing {k!r}")
        return cls(result_id=int(obj["result_id"]),
                   validator_wallet=str(obj["validator_wallet"]),
                   miner=SignedRequest.from_json(obj["miner"]),
                   verdict=SignedRequest.from_json(obj["verdict"]),
                   test_override_hit_status=obj.get("_test_override_hit_status"),
                   test_override_config_version=obj.get(
                       "_test_override_config_version"))

    def as_json_obj(self) -> dict[str, Any]:
        """The four committed fields, in the order closeDay() writes them."""
        return {"result_id": self.result_id,
                "validator_wallet": self.validator_wallet,
                "miner": self.miner.as_json_obj(),
                "verdict": self.verdict.as_json_obj()}


@dataclass(frozen=True)
class LogicalV2Record:
    """The assembled 14-field record of §17.3."""
    result_id: int
    wallet: str
    target: str
    molecule_hash: str
    archived_at: int
    coord_score: float
    validator_wallet: str
    validator_score: float | None
    coord_decision: str | None
    hit_status: str | None
    config_version: str
    bias_factor: float
    miner_req: str
    verdict_req: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id, "wallet": self.wallet,
            "target": self.target, "molecule_hash": self.molecule_hash,
            "archived_at": self.archived_at, "coord_score": self.coord_score,
            "validator_wallet": self.validator_wallet,
            "validator_score": self.validator_score,
            "coord_decision": self.coord_decision,
            "hit_status": self.hit_status,
            "config_version": self.config_version,
            "bias_factor": self.bias_factor,
            "miner_req": self.miner_req, "verdict_req": self.verdict_req,
        }


@dataclass
class V2Result:
    """The answer for one day: `accepted`, or the FIRST refusal code."""
    code: str
    rule: str | None = None
    detail: str = ""
    records: list[LogicalV2Record] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.code == ACCEPTED

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "rule": self.rule, "detail": self.detail,
                "recordCount": len(self.records)}


def _accept(records: list[LogicalV2Record]) -> V2Result:
    return V2Result(ACCEPTED, None, "all v2 rules passed", records)


def _refuse(code: str, rule: str, detail: str) -> V2Result:
    return V2Result(code, rule, detail)


# ── parsing helpers ─────────────────────────────────────────────────────────
def parse_signed_ndjson(raw: bytes) -> list[SignedLine]:
    out: list[SignedLine] = []
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("line is not a JSON object")
            out.append(SignedLine.from_json(obj))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValueError(
                f"signed.ndjson line {lineno} is unparseable: {exc}") from None
    return out


def signed_ndjson_bytes(lines: Sequence[SignedLine]) -> bytes:
    """Re-serialise signed.ndjson EXACTLY as closeDay() writes it.

    `JSON.stringify` with no spaces, newline-joined, one trailing newline when
    there is at least one line.  This is what `signed=` in the anchor memo
    commits to, so the separators must not drift.
    """
    body = "\n".join(
        json.dumps(sl.as_json_obj(), separators=(",", ":"), ensure_ascii=False)
        for sl in lines)
    return (body + "\n").encode("utf-8") if lines else b""


def signed_hash_hex(raw: bytes) -> str:
    return hashlib.sha256(bytes(raw)).hexdigest()


def day_from_archived_at(archived_at_ms: int) -> int:
    """§17.3: day_num = floor(archived_at / 86_400_000)."""
    return int(archived_at_ms) // 86_400_000


# ── signature verification ──────────────────────────────────────────────────
def verify_signature(wallet_b58: str, message: str, signature_hex: str) -> bool:
    """Ed25519, exactly as the Worker verifies it (§17 / auth §1).

    Signature.from_bytes(bytes.fromhex(sig)).verify(
        Pubkey.from_bytes(base58.b58decode(wallet)), message)
    """
    try:
        import base58
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        pub = Pubkey.from_bytes(base58.b58decode(wallet_b58))
        sig = Signature.from_bytes(bytes.fromhex(signature_hex))
        return bool(sig.verify(pub, message.encode("utf-8")))
    except Exception:
        return False


# ── the (wallet,nonce) / (wallet,molecule_hash,target) ledger ───────────────
class SignedRequestLedger:
    """§17 NO_DUPLICATE_SIGNED_REQUEST state, kept in the state folder.

    A SEPARATE file (`signed_request_ledger.json`) beside the existing
    payout ledger: `payout_index.json` and `payout_decisions.jsonl` are not
    touched, read or rewritten by this class, so adding v2 cannot disturb the
    replay guard that stops a batch being signed twice.

    Two keys, both from the change request:
      * (wallet, nonce)                         — a replayed signed request
      * (wallet, molecule_hash, target)         — the same work submitted twice
    """

    VERSION = 1
    FILENAME = "signed_request_ledger.json"

    def __init__(self, state_dir: str | Path | None):
        self.path = (Path(state_dir) / self.FILENAME) if state_dir else None
        self._nonces: set[tuple[str, str]] = set()
        self._work: set[tuple[str, str, str]] = set()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            obj = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(obj, dict) or obj.get("version") != self.VERSION:
            return
        for item in obj.get("wallet_nonce") or ():
            if isinstance(item, list) and len(item) == 2:
                self._nonces.add((str(item[0]), str(item[1])))
        for item in obj.get("wallet_molecule_target") or ():
            if isinstance(item, list) and len(item) == 3:
                self._work.add((str(item[0]), str(item[1]), str(item[2])))

    def save(self) -> None:
        if self.path is None or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"version": self.VERSION,
             "wallet_nonce": sorted(list(k) for k in self._nonces),
             "wallet_molecule_target": sorted(list(k) for k in self._work)},
            indent=1))
        tmp.replace(self.path)
        self._dirty = False

    # ── queries ─────────────────────────────────────────────────────────────
    def has_nonce(self, wallet: str, nonce: str) -> bool:
        return (wallet, nonce) in self._nonces

    def has_work(self, wallet: str, molecule_hash: str, target: str) -> bool:
        return (wallet, molecule_hash, target) in self._work

    def add_nonce(self, wallet: str, nonce: str) -> None:
        self._nonces.add((wallet, nonce))
        self._dirty = True

    def add_work(self, wallet: str, molecule_hash: str, target: str) -> None:
        self._work.add((wallet, molecule_hash, target))
        self._dirty = True

    def __len__(self) -> int:
        return len(self._nonces) + len(self._work)


# ── anchor memo /2 ──────────────────────────────────────────────────────────
_ANCHOR_V2_RE = re.compile(
    r"^LIFE-ANCHOR/2 day=(?P<day>\d{1,9}) date=(?P<date>\d{4}-\d{2}-\d{2}) "
    r"log=(?P<log>[0-9a-fA-F]{64}) signed=(?P<signed>[0-9a-fA-F]{64}) "
    r"program=(?P<program>[1-9A-HJ-NP-Za-km-z]{32,44})$")


def parse_anchor_memo_v2(text: str) -> dict[str, Any] | None:
    """Parse a LIFE-ANCHOR/2 memo.  None for anything not EXACTLY §17.4."""
    if not isinstance(text, str):
        return None
    m = _ANCHOR_V2_RE.match(text)
    if not m:
        return None
    return {"day": int(m.group("day")), "date": m.group("date"),
            "log_hash": m.group("log").lower(),
            "signed_hash": m.group("signed").lower(),
            "program_id": m.group("program")}


# ── rule evaluation ─────────────────────────────────────────────────────────
def evaluate_day(
    *,
    canonical_lines: Sequence[dict[str, Any]],
    signed_lines: Sequence[SignedLine],
    scoring_config: ScoringConfig,
    registry,
    expected_canonical_hash: str,
    expected_signed_hash: str,
    anchor_v2_memo: str,
    config_version: str | None = None,
    bias_factor: float = 1.0,
    expectations: dict[tuple[int, str], dict[str, Any]] | None = None,
    modality_of_target=None,
    ledger: SignedRequestLedger | None = None,
    validators_per_result: int = VALIDATORS_PER_RESULT,
    batch_mutation: str | None = None,
) -> V2Result:
    """Run EVERY v2 rule, in verify.ts order, and return the first refusal.

    `registry` answers `is_registered_miner(wallet)` and
    `is_validator_in_set(wallet)` — ChainRegistry on a live day,
    ManifestRegistry for the golden vectors (§17 golden README step 1).

    `expectations[(result_id, validator_wallet)]` optionally supplies the
    `coord_decision` / `hit_status` the day is claimed to have produced.  The
    golden vectors supply them from manifest.json; a production day has no
    committed copy, so the recomputed decision is compared with the signed
    `validator_verdict` in the verdict body instead and `hit_status` is only
    recomputed.
    """
    expectations = dict(expectations or {})
    cfg_version = config_version or scoring_config.coord_config_version

    # ── ANCHOR_V2_MATCH ────────────────────────────────────────────────────
    canonical_ndjson = "\n".join(
        json.dumps(l, separators=(",", ":"), ensure_ascii=False)
        for l in canonical_lines) + ("\n" if canonical_lines else "")
    computed_canon = hashlib.sha256(canonical_ndjson.encode("utf-8")).hexdigest()
    if computed_canon != expected_canonical_hash.lower():
        return _refuse(ANCHOR_V2_MISMATCH, "ANCHOR_V2_MATCH",
                       f"canonical_hash mismatch: computed {computed_canon[:16]}… "
                       f"expected {expected_canonical_hash[:16]}…")

    computed_signed = signed_hash_hex(signed_ndjson_bytes(signed_lines))
    if computed_signed != expected_signed_hash.lower():
        return _refuse(ANCHOR_V2_MISMATCH, "ANCHOR_V2_MATCH",
                       f"signed_hash mismatch: computed {computed_signed[:16]}… "
                       f"expected {expected_signed_hash[:16]}…")

    memo = parse_anchor_memo_v2(anchor_v2_memo)
    if memo is None:
        return _refuse(ANCHOR_V2_MISMATCH, "ANCHOR_V2_MATCH",
                       "LIFE-ANCHOR/2 memo missing or malformed")
    if memo["log_hash"] != computed_canon:
        return _refuse(ANCHOR_V2_MISMATCH, "ANCHOR_V2_MATCH",
                       f"anchor log= {memo['log_hash'][:16]}… != computed "
                       f"canonical hash {computed_canon[:16]}…")
    if memo["signed_hash"] != computed_signed:
        return _refuse(ANCHOR_V2_MISMATCH, "ANCHOR_V2_MATCH",
                       f"anchor signed= {memo['signed_hash'][:16]}… != computed "
                       f"signed hash {computed_signed[:16]}…")

    # ── DAY_NUM_DATE_CONSISTENT ────────────────────────────────────────────
    verdict_lines = [l for l in canonical_lines
                     if isinstance(l, dict) and "result_id" in l]
    for cl in verdict_lines:
        derived_day = day_from_archived_at(cl["archived_at"])
        derived_date = day_to_iso(derived_day)
        if derived_day != memo["day"] or derived_date != memo["date"]:
            return _refuse(
                ANCHOR_V2_MISMATCH, "DAY_NUM_DATE_CONSISTENT",
                f"result_id={cl['result_id']}: archived_at={cl['archived_at']} "
                f"→ day={derived_day} date={derived_date}, but the anchor says "
                f"day={memo['day']} date={memo['date']}")

    # ── SCORING_CONFIG_KNOWN ───────────────────────────────────────────────
    for sl in signed_lines:
        version = sl.test_override_config_version or cfg_version
        if not scoring_config.has_version(version):
            return _refuse(
                SCORING_CONFIG_UNKNOWN, "SCORING_CONFIG_KNOWN",
                f"config_version {version!r} is not in the pinned "
                f"scoring_config.json version_map "
                f"{scoring_config.versions()}")

    canon_by_id = {int(cl["result_id"]): cl for cl in verdict_lines}
    records: list[LogicalV2Record] = []

    # Nonce tracking.  The SAME miner request legitimately appears once per
    # validator of a result (§17.3 sorts by result_id then validator), so a
    # repeat is only a duplicate when the nonce DIFFERS for the same result.
    # A verdict request is one validator's own, so it must be globally unique.
    seen_miner_nonce: dict[int, str] = {}
    seen_verdict_nonce: set[tuple[str, str]] = set()
    seen_work: set[tuple[str, str, str]] = set()

    for sl in signed_lines:
        cl = canon_by_id.get(sl.result_id)
        if cl is None:
            return _refuse(
                ANCHOR_V2_MISMATCH, "SIGNED_LOG_PRESENT",
                f"signed.ndjson carries result_id={sl.result_id} with no "
                f"matching canonical line")

        # ── VALIDATOR_SELF_VERIFY ──────────────────────────────────────────
        # Before registration, so self-verification is named as such even
        # when the self-verifying wallet is not a registered validator.
        if sl.validator_wallet == sl.miner.wallet:
            return _refuse(
                VALIDATOR_SELF_VERIFY, "VALIDATOR_SELF_VERIFY",
                f"validator {sl.validator_wallet[:8]}… is the miner of "
                f"result_id={sl.result_id}")

        # ── MINER_IS_REGISTERED / VALIDATOR_IS_IN_SET ──────────────────────
        if not registry.is_registered_miner(sl.miner.wallet):
            return _refuse(
                WALLET_NOT_REGISTERED, "MINER_IS_REGISTERED",
                f"miner wallet {sl.miner.wallet[:8]}… is not a registered "
                f"miner on chain")
        if not registry.is_validator_in_set(sl.validator_wallet):
            return _refuse(
                WALLET_NOT_REGISTERED, "VALIDATOR_IS_IN_SET",
                f"validator wallet {sl.validator_wallet[:8]}… is not in the "
                f"on-chain validator set")

        # ── NO_DUPLICATE_SIGNED_REQUEST ────────────────────────────────────
        prev_miner_nonce = seen_miner_nonce.get(sl.result_id)
        if prev_miner_nonce is None:
            seen_miner_nonce[sl.result_id] = sl.miner.nonce
            if ledger is not None and ledger.has_nonce(sl.miner.wallet,
                                                       sl.miner.nonce):
                return _refuse(
                    DUPLICATE_SIGNED_REQUEST, "NO_DUPLICATE_SIGNED_REQUEST",
                    f"(wallet, nonce) for miner {sl.miner.wallet[:8]}… was "
                    f"already recorded on an earlier day")
        elif prev_miner_nonce != sl.miner.nonce:
            return _refuse(
                DUPLICATE_SIGNED_REQUEST, "NO_DUPLICATE_SIGNED_REQUEST",
                f"result_id={sl.result_id} carries two different miner "
                f"nonces in signed.ndjson")

        verdict_key = (sl.verdict.wallet, sl.verdict.nonce)
        if verdict_key in seen_verdict_nonce or (
                ledger is not None and ledger.has_nonce(*verdict_key)):
            return _refuse(
                DUPLICATE_SIGNED_REQUEST, "NO_DUPLICATE_SIGNED_REQUEST",
                f"duplicate (wallet, nonce): {sl.verdict.wallet[:8]}… nonce "
                f"{sl.verdict.nonce}")
        seen_verdict_nonce.add(verdict_key)

        work_key = (sl.miner.wallet, str(cl.get("molecule_hash")),
                    str(cl.get("target")))
        if prev_miner_nonce is None:
            if work_key in seen_work or (
                    ledger is not None and ledger.has_work(*work_key)):
                return _refuse(
                    DUPLICATE_SIGNED_REQUEST, "NO_DUPLICATE_SIGNED_REQUEST",
                    f"duplicate (wallet, molecule_hash, target): "
                    f"{sl.miner.wallet[:8]}… {work_key[1][:12]}… {work_key[2]}")
            seen_work.add(work_key)

        # ── TIMESTAMP_IN_RANGE ─────────────────────────────────────────────
        archived_sec = int(cl["archived_at"]) / 1000.0
        if not (archived_sec - MINER_TS_BACK_S <= sl.miner.timestamp
                <= archived_sec + MINER_TS_FORWARD_S):
            return _refuse(
                TIMESTAMP_OUT_OF_RANGE, "TIMESTAMP_IN_RANGE",
                f"miner.timestamp={sl.miner.timestamp} outside "
                f"[{archived_sec - MINER_TS_BACK_S}, "
                f"{archived_sec + MINER_TS_FORWARD_S}] "
                f"(archived_at={cl['archived_at']} ms)")
        if abs(sl.verdict.timestamp - archived_sec) > VERDICT_TS_WINDOW_S:
            return _refuse(
                TIMESTAMP_OUT_OF_RANGE, "TIMESTAMP_IN_RANGE",
                f"verdict.timestamp={sl.verdict.timestamp} outside "
                f"±{VERDICT_TS_WINDOW_S}s of archived_at/1000={archived_sec}")

        # ── MINER_SIG_VALID / VALIDATOR_SIG_VALID ──────────────────────────
        if not verify_signature(sl.miner.wallet, sl.miner.signed_message(),
                                sl.miner.signature):
            return _refuse(
                MINER_SIG_INVALID, "MINER_SIG_VALID",
                f"Ed25519 verification failed for miner "
                f"{sl.miner.wallet[:8]}… on result_id={sl.result_id}")
        if not verify_signature(sl.verdict.wallet, sl.verdict.signed_message(),
                                sl.verdict.signature):
            return _refuse(
                VALIDATOR_SIG_INVALID, "VALIDATOR_SIG_VALID",
                f"Ed25519 verification failed for validator "
                f"{sl.validator_wallet[:8]}… on result_id={sl.result_id}")

        try:
            miner_body = sl.miner.body_json()
            verdict_body = sl.verdict.body_json()
        except (ValueError, UnicodeDecodeError) as exc:
            return _refuse(
                SIGNED_DIGEST_MISMATCH, "SIGNED_DIGEST_MATCH",
                f"result_id={sl.result_id}: body_b64 does not decode as JSON "
                f"({exc})")

        # ── SCORE_BODY_MATCH (parsed doubles, §17 note 7) ──────────────────
        claimed = verdict_body.get("claimed_score")
        coord_score = float(cl["coord_score"])
        if isinstance(claimed, (int, float)) and not isinstance(claimed, bool):
            if abs(float(claimed) - coord_score) > 1e-9:
                return _refuse(
                    SCORE_BODY_MISMATCH, "SCORE_BODY_MATCH",
                    f"claimed_score={claimed} != canonical "
                    f"coord_score={coord_score}")
        miner_score = miner_body.get("score")
        if isinstance(miner_score, (int, float)) and not isinstance(miner_score, bool):
            if abs(float(miner_score) - coord_score) > 1e-9:
                return _refuse(
                    SCORE_BODY_MISMATCH, "SCORE_BODY_MATCH",
                    f"miner body score={miner_score} != canonical "
                    f"coord_score={coord_score}")

        # ── MOL_HASH_BODY_MATCH ────────────────────────────────────────────
        canon_mol = str(cl["molecule_hash"])
        if verdict_body.get("molecule_hash") and \
                verdict_body["molecule_hash"] != canon_mol:
            return _refuse(
                MOL_HASH_BODY_MISMATCH, "MOL_HASH_BODY_MATCH",
                f"verdict molecule_hash={verdict_body['molecule_hash']} != "
                f"canonical {canon_mol}")
        if miner_body.get("molecule_hash") and \
                miner_body["molecule_hash"] != canon_mol:
            return _refuse(
                MOL_HASH_BODY_MISMATCH, "MOL_HASH_BODY_MATCH",
                f"miner molecule_hash={miner_body['molecule_hash']} != "
                f"canonical {canon_mol}")

        # ── RESULT_ID_BODY_MATCH ───────────────────────────────────────────
        body_rid = verdict_body.get("result_id")
        if isinstance(body_rid, int) and not isinstance(body_rid, bool) \
                and body_rid != sl.result_id:
            return _refuse(
                RESULT_ID_BODY_MISMATCH, "RESULT_ID_BODY_MATCH",
                f"verdict body result_id={body_rid} != signed line "
                f"result_id={sl.result_id}")

        # ── TARGET_BODY_MATCH ──────────────────────────────────────────────
        canon_target = str(cl["target"])
        if miner_body.get("target") and miner_body["target"] != canon_target:
            return _refuse(
                TARGET_BODY_MISMATCH, "TARGET_BODY_MATCH",
                f"miner target={miner_body['target']!r} != canonical "
                f"{canon_target!r}")
        if verdict_body.get("target") and verdict_body["target"] != canon_target:
            return _refuse(
                TARGET_BODY_MISMATCH, "TARGET_BODY_MATCH",
                f"verdict target={verdict_body['target']!r} != canonical "
                f"{canon_target!r}")

        # ── MINER_WALLET_BODY_MATCH ────────────────────────────────────────
        canon_wallet = str(cl["wallet"])
        if verdict_body.get("miner_wallet") and \
                verdict_body["miner_wallet"] != canon_wallet:
            return _refuse(
                MINER_WALLET_BODY_MISMATCH, "MINER_WALLET_BODY_MATCH",
                f"verdict miner_wallet={verdict_body['miner_wallet'][:8]}… != "
                f"canonical wallet={canon_wallet[:8]}…")

        # ── DECISION_MATCH ─────────────────────────────────────────────────
        version = sl.test_override_config_version or cfg_version
        consts = scoring_config.constants(version)
        modality = (modality_of_target(canon_target) if modality_of_target
                    else None) or DEFAULT_MODALITY
        tolerance = consts.tolerance(modality, bias_factor != 1.0)
        validator_score = verdict_body.get("validator_score")
        if not isinstance(validator_score, (int, float)) or \
                isinstance(validator_score, bool):
            return _refuse(
                SCORE_BODY_MISMATCH, "SCORE_BODY_MATCH",
                f"verdict body carries no numeric validator_score "
                f"(result_id={sl.result_id})")
        validator_score = float(validator_score)
        adjusted = coord_score * bias_factor
        rel_err = (abs(validator_score - adjusted) / abs(adjusted)
                   if adjusted else abs(validator_score))
        computed_decision = "confirm" if rel_err <= tolerance else "reject"

        exp = expectations.get((sl.result_id, sl.validator_wallet))
        if exp is not None and exp.get("coord_decision") is not None:
            expected_decision = str(exp["coord_decision"])
        else:
            # No committed copy: the signed `validator_verdict` is the claim.
            vv = verdict_body.get("validator_verdict")
            expected_decision = str(vv) if vv in ("confirm", "reject") \
                else computed_decision
        if computed_decision != expected_decision:
            return _refuse(
                DECISION_MISMATCH, "DECISION_MATCH",
                f"result_id={sl.result_id}: recomputed {computed_decision}, "
                f"claimed {expected_decision} (rel_err={rel_err:.4f} "
                f"tol={tolerance} modality={modality})")

        # ── HIT_MATCH ──────────────────────────────────────────────────────
        threshold = consts.hit_threshold(canon_target)
        computed_hit: str | None = None
        if computed_decision == "confirm":
            computed_hit = ("confirmed_hit" if threshold is None
                            else ("confirmed_hit" if validator_score <= threshold
                                  else "confirmed_not_hit"))
        expected_hit = sl.test_override_hit_status
        if expected_hit is None and exp is not None:
            expected_hit = exp.get("hit_status")
        if expected_hit is not None and computed_hit != expected_hit:
            return _refuse(
                HIT_MISMATCH, "HIT_MATCH",
                f"result_id={sl.result_id}: recomputed {computed_hit!r}, "
                f"claimed {expected_hit!r} (threshold={threshold}, "
                f"validator_score={validator_score})")

        records.append(LogicalV2Record(
            result_id=sl.result_id, wallet=canon_wallet, target=canon_target,
            molecule_hash=canon_mol, archived_at=int(cl["archived_at"]),
            coord_score=coord_score, validator_wallet=sl.validator_wallet,
            validator_score=validator_score, coord_decision=computed_decision,
            hit_status=computed_hit, config_version=version,
            bias_factor=bias_factor, miner_req=sl.miner.digest(),
            verdict_req=sl.verdict.digest()))

    # ── VALIDATOR_OVER_LIMIT ───────────────────────────────────────────────
    per_result: dict[int, int] = {}
    for sl in signed_lines:
        per_result[sl.result_id] = per_result.get(sl.result_id, 0) + 1
    for rid, count in sorted(per_result.items()):
        if count > validators_per_result:
            return _refuse(
                VALIDATOR_OVER_LIMIT, "VALIDATOR_OVER_LIMIT",
                f"result_id={rid} has {count} signed entries, above "
                f"K=VALIDATORS_PER_RESULT={validators_per_result}")

    # ── VALIDATOR_PAY_MATCH ────────────────────────────────────────────────
    # The pay rule itself lives in payout.validator_pay and is checked against
    # the BATCH, not the log.  The golden vectors signal a pay mutation with a
    # `_test_batch_mutation` marker because they carry no batch file.
    if batch_mutation == "validator_amount_halved":
        return _refuse(
            VALIDATOR_PAY_MISMATCH, "VALIDATOR_PAY_MATCH",
            "batch mutation: a validator entry's amountRaw is not the miner "
            "amountRaw for that result")

    return _accept(records)


def commit_day_to_ledger(signed_lines: Iterable[SignedLine],
                         canonical_lines: Sequence[dict[str, Any]],
                         ledger: SignedRequestLedger) -> None:
    """Record an ACCEPTED day's (wallet, nonce) / (wallet, mol, target) keys.

    Called only after `evaluate_day` accepted: a refused day must leave the
    ledger untouched, or a re-run would refuse it for its own earlier entries.
    """
    canon_by_id = {int(cl["result_id"]): cl for cl in canonical_lines
                   if isinstance(cl, dict) and "result_id" in cl}
    for sl in signed_lines:
        ledger.add_nonce(sl.miner.wallet, sl.miner.nonce)
        ledger.add_nonce(sl.verdict.wallet, sl.verdict.nonce)
        cl = canon_by_id.get(sl.result_id)
        if cl is not None:
            ledger.add_work(sl.miner.wallet, str(cl.get("molecule_hash")),
                            str(cl.get("target")))
    ledger.save()
