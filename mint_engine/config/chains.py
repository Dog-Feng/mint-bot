from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChainPreset:
    key: str
    name: str
    chain_id: int
    native_symbol: str
    gas_type: str = "eip1559"
    explorer: str | None = None
    explorer_api: str | None = None
    sourcify: bool = True
    public_rpc: str | None = None
    public_rpcs: tuple[str, ...] = field(default_factory=tuple)
    has_l1_data_fee: bool = False
    has_public_mempool: bool = True
    etherscan_chain_id: int | None = None


CHAINS: dict[int, ChainPreset] = {
    1: ChainPreset(
        key="ethereum",
        name="Ethereum",
        chain_id=1,
        native_symbol="ETH",
        explorer="https://etherscan.io",
        explorer_api="https://api.etherscan.io/v2/api",
        public_rpc="https://1rpc.io/eth",
        public_rpcs=(
            "https://1rpc.io/eth",
            "https://ethereum.publicnode.com",
            "https://cloudflare-eth.com",
        ),
        etherscan_chain_id=1,
        has_public_mempool=True,
    ),
    8453: ChainPreset(
        key="base",
        name="Base",
        chain_id=8453,
        native_symbol="ETH",
        explorer="https://basescan.org",
        explorer_api="https://api.etherscan.io/v2/api",
        public_rpc="https://mainnet.base.org",
        public_rpcs=(
            "https://mainnet.base.org",
            "https://base.publicnode.com",
            "https://1rpc.io/base",
        ),
        etherscan_chain_id=8453,
        has_l1_data_fee=True,
    ),
    42161: ChainPreset(
        key="arbitrum",
        name="Arbitrum One",
        chain_id=42161,
        native_symbol="ETH",
        explorer="https://arbiscan.io",
        explorer_api="https://api.etherscan.io/v2/api",
        public_rpc="https://arb1.arbitrum.io/rpc",
        public_rpcs=(
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum-one.publicnode.com",
            "https://1rpc.io/arb",
        ),
        etherscan_chain_id=42161,
    ),
    10: ChainPreset(
        key="optimism",
        name="Optimism",
        chain_id=10,
        native_symbol="ETH",
        explorer="https://optimistic.etherscan.io",
        explorer_api="https://api.etherscan.io/v2/api",
        public_rpc="https://mainnet.optimism.io",
        public_rpcs=(
            "https://mainnet.optimism.io",
            "https://optimism.publicnode.com",
            "https://1rpc.io/op",
        ),
        etherscan_chain_id=10,
        has_l1_data_fee=True,
    ),
    56: ChainPreset(
        key="bsc",
        name="BSC",
        chain_id=56,
        native_symbol="BNB",
        explorer="https://bscscan.com",
        explorer_api="https://api.etherscan.io/v2/api",
        public_rpc="https://bsc-dataseed.binance.org",
        public_rpcs=(
            "https://bsc-dataseed.binance.org",
            "https://bsc.publicnode.com",
            "https://1rpc.io/bnb",
        ),
        etherscan_chain_id=56,
    ),
    137: ChainPreset(
        key="polygon",
        name="Polygon",
        chain_id=137,
        native_symbol="POL",
        explorer="https://polygonscan.com",
        explorer_api="https://api.etherscan.io/v2/api",
        public_rpc="https://polygon-rpc.com",
        public_rpcs=(
            "https://polygon-rpc.com",
            "https://polygon-bor.publicnode.com",
            "https://1rpc.io/matic",
        ),
        etherscan_chain_id=137,
    ),
    4663: ChainPreset(
        key="robinhood",
        name="Robinhood Chain",
        chain_id=4663,
        native_symbol="ETH",
        explorer="https://robinhoodchain.blockscout.com",
        explorer_api="https://robinhoodchain.blockscout.com/api",
        public_rpc="https://rpc.mainnet.chain.robinhood.com",
        public_rpcs=("https://rpc.mainnet.chain.robinhood.com",),
        sourcify=True,
        has_public_mempool=False,
        etherscan_chain_id=None,
    ),
    5042: ChainPreset(
        key="arc",
        name="Arc",
        chain_id=5042,
        native_symbol="USDC",
        gas_type="eip1559",
        explorer="https://explorer.arc.io",
        explorer_api="https://explorer.arc.io/api",
        public_rpc="https://rpc.mainnet.arc.io",
        public_rpcs=(
            "https://rpc.mainnet.arc.io",
            "https://rpc.drpc.mainnet.arc.io",
        ),
        sourcify=True,
        has_public_mempool=True,
        etherscan_chain_id=None,
    ),
}


OPENSEA_CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "eth": 1,
    "base": 8453,
    "arbitrum": 42161,
    "arbitrum_one": 42161,
    "optimism": 10,
    "matic": 137,
    "polygon": 137,
    "bsc": 56,
    "bnb": 56,
    "robinhood": 4663,
    "arc": 5042,
}


def get_chain(chain_id: int) -> ChainPreset:
    try:
        return CHAINS[chain_id]
    except KeyError as exc:
        raise ValueError(f"unsupported chain_id: {chain_id}") from exc


def public_rpc_urls(chain_id: int) -> list[str]:
    chain = get_chain(chain_id)
    urls: list[str] = []
    for url in (*chain.public_rpcs, chain.public_rpc):
        if url and url not in urls:
            urls.append(url)
    return urls


def chain_id_from_opensea(name: str) -> int:
    key = (name or "").strip().lower().replace("-", "_")
    if key not in OPENSEA_CHAIN_IDS:
        raise ValueError(f"unsupported OpenSea chain: {name}")
    return OPENSEA_CHAIN_IDS[key]
