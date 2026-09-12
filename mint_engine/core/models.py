from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Capability(str, Enum):
    AUTO = "AUTO"
    SEMI_AUTO = "SEMI_AUTO"
    INTEGRATION_REQUIRED = "INTEGRATION_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"


class SaleStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    PAUSED = "PAUSED"
    SOLD_OUT = "SOLD_OUT"
    WALLET_LIMIT_REACHED = "WALLET_LIMIT_REACHED"
    UNKNOWN = "UNKNOWN"


class WalletStatus(str, Enum):
    READY = "READY"
    CHECKING = "CHECKING"
    INSUFFICIENT_ETH = "INSUFFICIENT_ETH"
    ALREADY_MINTED = "ALREADY_MINTED"
    WALLET_LIMIT = "WALLET_LIMIT"
    SKIPPED = "SKIPPED"
    MISSING_KEY = "MISSING_KEY"


class RunMode(str, Enum):
    INSPECT = "inspect"
    DRY_RUN = "dry_run"
    MINT = "mint"


class StartStrategy(str, Enum):
    IMMEDIATE = "immediate"
    SCHEDULED = "scheduled"


class AbiSource(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class WalletItem(BaseModel):
    label: str = "wallet"
    private_key: str | None = None
    address: str | None = None

    @field_validator("private_key")
    @classmethod
    def strip_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class RpcConfig(BaseModel):
    urls: list[str] = Field(default_factory=list)
    probe_on_start: bool = True
    probe_timeout_ms: int = 1500
    min_healthy: int = 1
    selection: str = "auto"
    primary: str | None = None
    broadcast_backups: bool = True


class WalletConfig(BaseModel):
    source: str = "pasted"
    items: list[WalletItem] = Field(default_factory=list)
    concurrency: int = 8
    stop_on_sold_out: bool = True
    skip_if_already_minted: bool = True


class AbiConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: AbiSource = AbiSource.AUTO
    payload: Any | None = Field(default=None, alias="json")


class MintConfig(BaseModel):
    contract: str | None = None
    opensea_url: str | None = None
    quantity: int = 1
    recipient_mode: str = "self"
    recipient: str | None = None
    abi: AbiConfig = Field(default_factory=AbiConfig)
    extra_params: dict[str, Any] = Field(default_factory=dict)


class GasBump(BaseModel):
    mode: str = "extra_tip"
    extra_priority_gwei: float = 1.0  # 0 = 不加额外 tip，只用链上 tip
    max_fee_multiplier: float = 1.0  # <=0 = 1.0，即 baseFee + tip
    hard_cap_gwei: float = 0  # 0 = 不设硬顶
    fixed_priority_gwei: float | None = None


class GasConfig(BaseModel):
    type: str = "eip1559"
    bump: GasBump = Field(default_factory=GasBump)
    gas_limit_mode: str = "estimate_or_fallback"
    fallback_gas_limit: int = 280000
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_priority_multiplier: float = 1.5
    retry_max_fee_multiplier: float = 1.25
    receipt_timeout_sec: float = 180


class ScheduleConfig(BaseModel):
    start_time_unix: int | None = None
    prepare_lead_sec: int = 60
    sign_lead_sec: int = 20
    final_check_lead_sec: int = 5


class SafetyConfig(BaseModel):
    dry_run_required: bool = True
    allow_skip_simulation: bool = False
    never_log_private_keys: bool = True


class ChainConfig(BaseModel):
    key: str | None = None
    chain_id: int | None = None
    native_symbol: str | None = None


class RunMeta(BaseModel):
    name: str = "mint"
    mode: RunMode = RunMode.INSPECT
    start_strategy: StartStrategy = StartStrategy.SCHEDULED


class RunConfig(BaseModel):
    run: RunMeta = Field(default_factory=RunMeta)
    chain: ChainConfig = Field(default_factory=ChainConfig)
    rpc: RpcConfig = Field(default_factory=RpcConfig)
    wallets: WalletConfig = Field(default_factory=WalletConfig)
    mint: MintConfig
    gas: GasConfig = Field(default_factory=GasConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    @model_validator(mode="after")
    def require_contract_or_opensea(self) -> "RunConfig":
        contract = (self.mint.contract or "").strip()
        url = (self.mint.opensea_url or "").strip()
        looks_opensea = "opensea.io" in contract.lower()
        if not contract and not url:
            raise ValueError("need mint.contract or mint.opensea_url")
        if not url and not looks_opensea and self.chain.chain_id is None:
            raise ValueError("need chain.chain_id when not using an OpenSea URL")
        return self


class ProbeResult(BaseModel):
    url: str
    latency_ms: int | None = None
    block_number: int | None = None
    chain_id: int | None = None
    status: str
    role: str = "unused"
    error: str | None = None
    score: float = 0.0


class SaleState(BaseModel):
    active: bool = False
    status: SaleStatus = SaleStatus.UNKNOWN
    start_time: int | None = None
    end_time: int | None = None
    price: int = 0
    max_per_wallet: int | None = None
    remaining_supply: int | None = None
    total_supply: int | None = None
    max_supply: int | None = None
    wallet_minted: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class MintMethod(BaseModel):
    name: str
    signature: str
    selector: str
    to: str
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    payable: bool = True
    confidence: float
    source: str


class Gap(BaseModel):
    key: str
    message: str
    blocking: bool = True
    candidates: list[Any] = Field(default_factory=list)


class WalletReady(BaseModel):
    label: str
    address: str
    status: WalletStatus
    balance_wei: int = 0
    nonce: int | None = None
    minted: int | None = None
    note: str | None = None


class TxPlan(BaseModel):
    to: str
    data: str
    value: int
    gas: int
    max_fee_per_gas: int | None = None
    max_priority_fee_per_gas: int | None = None
    gas_price: int | None = None
    nonce: int | None = None
    chain_id: int
    from_address: str | None = None


class OpenSeaPreview(BaseModel):
    slug: str
    url: str
    chain: str
    chain_id: int
    contract: str
    contracts: list[dict[str, str]] = Field(default_factory=list)
    collection_name: str | None = None
    drop_type: str | None = None
    is_minting: bool | None = None
    max_supply: str | None = None
    total_supply: str | None = None
    next_stage: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SweepRequest(BaseModel):
    chain_id: int
    contract: str
    destination: str | None = None
    mode: str = "scan"
    token_ids: list[str] = Field(default_factory=list)
    last_mint: dict[str, list[str]] = Field(default_factory=dict)
    standard: str = "auto"
    concurrency: int = Field(default=2, ge=1, le=8)
    max_per_wallet: int = Field(default=0, ge=0)
    rpc_urls: list[str] = Field(default_factory=list)
    wallets: list[WalletItem] = Field(default_factory=list)
    gas: GasConfig = Field(default_factory=GasConfig)


class InspectReport(BaseModel):
    chain_id: int
    chain_name: str
    contract: str
    is_proxy: bool
    implementation: str | None = None
    proxy_type: str | None = None
    contract_type: str = "Unknown"
    protocol: str | None = None
    contract_name: str | None = None
    symbol: str | None = None
    abi_source: str
    abi_function_count: int = 0
    capability: Capability
    method: MintMethod | None = None
    candidates: list[MintMethod] = Field(default_factory=list)
    sale: SaleState
    value: int = 0
    gaps: list[Gap] = Field(default_factory=list)
    wallets: list[WalletReady] = Field(default_factory=list)
    rpc: list[ProbeResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    opensea: OpenSeaPreview | None = None
