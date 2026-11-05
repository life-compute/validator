"""On-chain registration facts, decoded from the program's own Rust layouts.

Two questions the C47 checker has to answer for every signed line
(CHECKER_CHANGE_REQUEST_C47 rules MINER_IS_REGISTERED / VALIDATOR_IS_IN_SET):

  * is `miner.wallet` a REGISTERED miner?
  * is `verdict.wallet` in the VALIDATOR SET?

Both are read from the chain, never from the coordinator.  The layouts are
transcribed from `programs/core/src/state/*.rs` as copied into the C47 spec
bundle; the byte offsets below are the ones those files document, and the
decoder asserts the account length the Rust `LEN` constant gives.

MinerAccount — PDA seeds [b"miner", wallet], 128 bytes (miner_account.rs):

    [  0..  7]  discriminator           (8)
    [  8.. 39]  owner                   (32)
    [ 40.. 47]  total_life_earned       (u64 LE)
    [ 48.. 55]  molecules_screened      (u64 LE)
    [ 56.. 63]  last_epoch              (u64 LE)
    [ 64]       is_registered           (bool)
    [ 65]       bump                    (u8)
    [ 66]       submission_count        (u8)
    [ 67.. 74]  submission_epoch        (u64 LE)
    [ 75.. 97]  _reserved               (23)
    [ 98]       stake_returned          (bool)
    [ 99]       failed_verification     (bool)
    [100]       open_dispute            (bool)
    [101..108]  stake_locked_until_slot (u64 LE)
    [109..127]  _reserved_c4            (19)

NetworkConfig — PDA [b"network_config"], 344 bytes (network_config.rs).  Its
field order gives these offsets:

    [  0..  7]  discriminator           (8)
    [  8.. 39]  authority               (32)
    [ 40.. 71]  life_mint               (32)
    [ 72.. 79]  supply_cap              (u64 LE)
    [ 80.. 87]  total_minted            (u64 LE)
    [ 88.. 95]  current_epoch           (u64 LE)
    [ 96..103]  epoch_start_slot        (i64 LE)
    [104..111]  epoch_duration_slots    (u64 LE)
    [112]       validators_required     (u8)
    [113..116]  validation_tolerance    (f32 LE)
    [117..276]  validators              ([Pubkey; 5] = 160)
    [277]       validator_count         (u8)
    [278]       bump                    (u8)
    [279]       mint_authority_bump     (u8)
    [280..287]  total_miners_registered (u64 LE)
    [288..295]  total_validators_registered (u64 LE)
    [296..343]  padding                 (48)

`NetworkConfig::is_validator` is `validators[..validator_count].contains(k)`,
so only the first `validator_count` slots count — a stale key left in a later
slot is NOT a member.

ValidatorAccount (PDA [b"validator_account", wallet], validator_account.rs)
is decoded too when it exists, for reputation reporting.  It is NOT the
membership authority: the program's own `is_validator` reads NetworkConfig,
and on devnet a registered validator can have no ValidatorAccount yet (one is
created on its first `validate_result`).

FAIL CLOSED, NEVER FAIL OPEN.  An RPC that cannot be read raises
AnchorRpcUnavailable (transient, same treatment as an unreadable anchor); an
account that is absent or malformed is "not registered", which is a refusal.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any

from .batch_hash import b58decode, pubkey_bytes
from .chain_anchors import AnchorRpcUnavailable, SolanaRpc

# Anchor PDA derivation needs the curve check; solders is already a hard
# dependency of the validator venv (coordinator_client.signing uses it).
MINER_SEED = b"miner"
NETWORK_CONFIG_SEED = b"network_config"
VALIDATOR_ACCOUNT_SEED = b"validator_account"

MINER_ACCOUNT_LEN = 128
NETWORK_CONFIG_LEN = 344
VALIDATOR_ACCOUNT_LEN = 104

# NetworkConfig offsets (see the table above).
_NC_VALIDATORS_OFF = 8 + 32 + 32 + 8 + 8 + 8 + 8 + 8 + 1 + 4     # 117
_NC_VALIDATOR_COUNT_OFF = _NC_VALIDATORS_OFF + 32 * 5            # 277


class ChainStateError(Exception):
    """An account exists but does not decode as the layout says it should."""


def _b58encode(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = alphabet[rem] + out
    for b in raw:
        if b != 0:
            break
        out = "1" + out
    return out or "1"


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Anchor/Solana PDA derivation.  Uses solders, the same library the
    validator signs with, so a Python-only reimplementation cannot drift."""
    from solders.pubkey import Pubkey
    pda, bump = Pubkey.find_program_address(
        seeds, Pubkey.from_string(program_id))
    return str(pda), int(bump)


def miner_pda(wallet: str, program_id: str) -> str:
    return find_program_address(
        [MINER_SEED, pubkey_bytes(wallet)], program_id)[0]


def validator_account_pda(wallet: str, program_id: str) -> str:
    return find_program_address(
        [VALIDATOR_ACCOUNT_SEED, pubkey_bytes(wallet)], program_id)[0]


# ── decoders ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MinerAccount:
    owner: str
    total_life_earned: int
    molecules_screened: int
    last_epoch: int
    is_registered: bool
    bump: int
    submission_count: int
    submission_epoch: int
    stake_returned: bool
    failed_verification: bool
    open_dispute: bool
    stake_locked_until_slot: int

    @classmethod
    def decode(cls, data: bytes) -> "MinerAccount":
        if len(data) != MINER_ACCOUNT_LEN:
            raise ChainStateError(
                f"MinerAccount must be {MINER_ACCOUNT_LEN} bytes "
                f"(miner_account.rs LEN), got {len(data)}")
        return cls(
            owner=_b58encode(data[8:40]),
            total_life_earned=struct.unpack_from("<Q", data, 40)[0],
            molecules_screened=struct.unpack_from("<Q", data, 48)[0],
            last_epoch=struct.unpack_from("<Q", data, 56)[0],
            is_registered=bool(data[64]),
            bump=data[65],
            submission_count=data[66],
            submission_epoch=struct.unpack_from("<Q", data, 67)[0],
            stake_returned=bool(data[98]),
            failed_verification=bool(data[99]),
            open_dispute=bool(data[100]),
            stake_locked_until_slot=struct.unpack_from("<Q", data, 101)[0],
        )

    def as_dict(self) -> dict[str, Any]:
        return {"owner": self.owner, "isRegistered": self.is_registered,
                "bump": self.bump, "lastEpoch": self.last_epoch,
                "moleculesScreened": self.molecules_screened,
                "totalLifeEarned": self.total_life_earned,
                "stakeReturned": self.stake_returned,
                "failedVerification": self.failed_verification}


@dataclass(frozen=True)
class NetworkConfig:
    authority: str
    life_mint: str
    validators_required: int
    validation_tolerance: float
    validators: tuple[str, ...]
    validator_count: int
    total_miners_registered: int
    total_validators_registered: int

    @classmethod
    def decode(cls, data: bytes) -> "NetworkConfig":
        if len(data) != NETWORK_CONFIG_LEN:
            raise ChainStateError(
                f"NetworkConfig must be {NETWORK_CONFIG_LEN} bytes "
                f"(network_config.rs LEN), got {len(data)}")
        vals = tuple(
            _b58encode(data[_NC_VALIDATORS_OFF + 32 * i:
                            _NC_VALIDATORS_OFF + 32 * (i + 1)])
            for i in range(5))
        return cls(
            authority=_b58encode(data[8:40]),
            life_mint=_b58encode(data[40:72]),
            validators_required=data[112],
            validation_tolerance=struct.unpack_from("<f", data, 113)[0],
            validators=vals,
            validator_count=data[_NC_VALIDATOR_COUNT_OFF],
            total_miners_registered=struct.unpack_from("<Q", data, 280)[0],
            total_validators_registered=struct.unpack_from("<Q", data, 288)[0],
        )

    def is_validator(self, wallet: str) -> bool:
        """The program's own rule: only the first `validator_count` slots."""
        return wallet in self.validators[:self.validator_count]

    def active_validators(self) -> tuple[str, ...]:
        return self.validators[:self.validator_count]

    def as_dict(self) -> dict[str, Any]:
        return {"authority": self.authority, "lifeMint": self.life_mint,
                "validatorsRequired": self.validators_required,
                "validatorCount": self.validator_count,
                "validators": list(self.active_validators()),
                "totalMinersRegistered": self.total_miners_registered,
                "totalValidatorsRegistered": self.total_validators_registered}


@dataclass(frozen=True)
class ValidatorAccount:
    validator: str
    total_validations: int
    confirmations: int
    reputation_bps: int
    last_active_slot: int
    is_active: bool
    bump: int

    @classmethod
    def decode(cls, data: bytes) -> "ValidatorAccount":
        if len(data) != VALIDATOR_ACCOUNT_LEN:
            raise ChainStateError(
                f"ValidatorAccount must be {VALIDATOR_ACCOUNT_LEN} bytes "
                f"(validator_account.rs LEN), got {len(data)}")
        return cls(
            validator=_b58encode(data[8:40]),
            total_validations=struct.unpack_from("<Q", data, 40)[0],
            confirmations=struct.unpack_from("<Q", data, 48)[0],
            reputation_bps=struct.unpack_from("<H", data, 56)[0],
            last_active_slot=struct.unpack_from("<q", data, 58)[0],
            is_active=bool(data[66]),
            bump=data[67],
        )


# ── the reader ──────────────────────────────────────────────────────────────
class ChainRegistry:
    """Reads miner registration and validator membership from the chain.

    Results are memoised per wallet for the life of the object, so one run
    asks the RPC once per wallet however many signed lines mention it.
    """

    def __init__(self, program_id: str, *, rpc: SolanaRpc | None = None,
                 rpc_url: str | None = None, timeout: float = 20.0,
                 headers: dict[str, str] | None = None):
        if not (program_id or "").strip():
            raise ValueError("PROGRAM_ID is required to read on-chain state")
        self.program_id = program_id.strip()
        if rpc is not None:
            self.rpc = rpc
        else:
            from .chain_anchors import DEFAULT_ANCHOR_RPC_URL
            self.rpc = SolanaRpc(rpc_url or DEFAULT_ANCHOR_RPC_URL,
                                 timeout=timeout, headers=headers)
        self._miners: dict[str, MinerAccount | None] = {}
        self._network: NetworkConfig | None = None
        self._network_read = False

    # ── raw account fetch ───────────────────────────────────────────────────
    def _account_data(self, pubkey: str) -> bytes | None:
        res = self.rpc.call("getAccountInfo",
                            [pubkey, {"encoding": "base64",
                                      "commitment": "finalized"}])
        value = (res or {}).get("value") if isinstance(res, dict) else None
        if not isinstance(value, dict):
            return None
        data = value.get("data")
        if not (isinstance(data, list) and data and isinstance(data[0], str)):
            return None
        try:
            return base64.b64decode(data[0])
        except Exception:
            raise ChainStateError(
                f"account {pubkey[:8]}… data is not valid base64") from None

    # ── miner ───────────────────────────────────────────────────────────────
    def miner_account(self, wallet: str) -> MinerAccount | None:
        if wallet in self._miners:
            return self._miners[wallet]
        try:
            raw = self._account_data(miner_pda(wallet, self.program_id))
        except ValueError:
            # Not a decodable base58 pubkey at all: it cannot be registered.
            self._miners[wallet] = None
            return None
        acct = MinerAccount.decode(raw) if raw is not None else None
        self._miners[wallet] = acct
        return acct

    def is_registered_miner(self, wallet: str) -> bool:
        """`is_registered` on the miner's own PDA — the program's own flag."""
        acct = self.miner_account(wallet)
        return bool(acct is not None and acct.is_registered
                    and acct.owner == wallet)

    # ── validator ───────────────────────────────────────────────────────────
    def network_config(self) -> NetworkConfig | None:
        if self._network_read:
            return self._network
        self._network_read = True
        res = self.rpc.call("getProgramAccounts", [
            self.program_id,
            {"encoding": "base64", "commitment": "finalized",
             "filters": [{"dataSize": NETWORK_CONFIG_LEN}]}])
        if not isinstance(res, list):
            raise AnchorRpcUnavailable(
                "getProgramAccounts(NetworkConfig) returned no list")
        for item in res:
            try:
                data = base64.b64decode(item["account"]["data"][0])
                self._network = NetworkConfig.decode(data)
                return self._network
            except (KeyError, TypeError, ValueError, ChainStateError):
                continue
        return None

    def is_validator_in_set(self, wallet: str) -> bool:
        nc = self.network_config()
        return bool(nc is not None and nc.is_validator(wallet))

    def validator_account(self, wallet: str) -> ValidatorAccount | None:
        try:
            raw = self._account_data(
                validator_account_pda(wallet, self.program_id))
        except ValueError:
            return None
        return ValidatorAccount.decode(raw) if raw is not None else None


class ManifestRegistry:
    """The GOLDEN-VECTOR registry: `registered_wallets` from manifest.json.

    §17 golden README step 1: for the golden vectors the on-chain lookup is
    replaced by the manifest's own list, because the synthetic wallets have
    no accounts on any cluster.  Same interface as ChainRegistry, so the rule
    evaluator never knows which one it is holding.
    """

    def __init__(self, registered_wallets: list[dict[str, Any]]):
        self._miners: set[str] = set()
        self._validators: set[str] = set()
        for row in registered_wallets or ():
            if not isinstance(row, dict):
                continue
            wallet = str(row.get("wallet") or "")
            if not wallet:
                continue
            role = str(row.get("role") or "")
            is_validator = bool(row.get("is_validator"))
            # verify.ts: a wallet is a MINER when it is not a validator, or
            # when its role says it is both.
            if not is_validator or role == "miner+validator":
                self._miners.add(wallet)
            if is_validator:
                self._validators.add(wallet)

    def is_registered_miner(self, wallet: str) -> bool:
        return wallet in self._miners

    def is_validator_in_set(self, wallet: str) -> bool:
        return wallet in self._validators
