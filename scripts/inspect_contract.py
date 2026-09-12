from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mint_engine.controller import MintController
from mint_engine.core.models import ChainConfig, MintConfig, RpcConfig, RunConfig, WalletConfig, WalletItem
from mint_engine.discovery.opensea import prepare_config


async def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a mint contract")
    parser.add_argument("--chain-id", type=int)
    parser.add_argument("--contract")
    parser.add_argument("--opensea")
    parser.add_argument("--rpc", action="append", default=[])
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--wallet", action="append", default=[])
    args = parser.parse_args()

    config = RunConfig(
        chain=ChainConfig(chain_id=args.chain_id),
        rpc=RpcConfig(urls=args.rpc),
        mint=MintConfig(
            contract=args.contract,
            opensea_url=args.opensea,
            quantity=args.quantity,
        ),
        wallets=WalletConfig(
            items=[WalletItem(label=f"wallet_{i+1}", address=addr) for i, addr in enumerate(args.wallet)]
        ),
    )
    config, preview = await prepare_config(config)
    controller = MintController(config, preview)
    try:
        report = await controller.inspect()
        print(json.dumps(report.model_dump(), indent=2, ensure_ascii=False, default=str))
    finally:
        await controller.aclose()


if __name__ == "__main__":
    asyncio.run(main())
