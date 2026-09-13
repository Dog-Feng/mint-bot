from __future__ import annotations

import asyncio
from typing import Any

import httpx
from eth_utils import keccak

from mint_engine.config.chains import ChainPreset, get_chain, public_rpc_urls
from mint_engine.config.settings import get_settings
from mint_engine.core.exceptions import ConfigError
from mint_engine.core.models import SweepRequest, TxPlan
from mint_engine.evm import (
    ERC1155_IID,
    ERC721_IID,
    checksum,
    decode_address,
    decode_data,
    decode_uint,
    encode_call,
    is_address,
)
from mint_engine.rpc.pool import RpcPool
from mint_engine.transaction.gas import quote_gas, resolve_gas_limit
from mint_engine.transaction.signer import sign_tx
from mint_engine.wallet.manager import ResolvedWallet, WalletManager

TRANSFER_721 = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
ERC721_ENUMERABLE_IID = "0x780e9d63"
EXPLORER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; mint-engine/0.1)",
    "Accept": "application/json",
}
LOG_LOOKBACK = 80_000
LOG_CHUNK = 2_000
LOG_CHUNK_MIN = 200


def _topic_address(address: str) -> str:
    return "0x" + checksum(address)[2:].lower().zfill(64)


def _as_int(value: str | int) -> int:
    text = str(value).strip()
    if text.startswith(("0x", "0X")):
        return int(text, 16)
    return int(text)


def _ids_from_map(last_mint: dict[str, list[str]], address: str) -> list[int]:
    found: list[int] = []
    target = address.lower()
    for key, values in (last_mint or {}).items():
        if (key or "").lower() == target:
            for item in values:
                try:
                    found.append(_as_int(item))
                except Exception:
                    continue
    return found


async def _open_pool(chain_id: int, rpc_urls: list[str]) -> RpcPool:
    chain = get_chain(chain_id)
    preferred = [url.strip() for url in rpc_urls if url and url.strip()]
    same_chain = public_rpc_urls(chain.chain_id)
    urls = preferred or same_chain
    if not urls:
        raise ConfigError(f"chain {chain.chain_id} 没有可用 RPC")
    pool = RpcPool(urls, chain.chain_id, timeout_ms=4000)
    try:
        await pool.probe()
        return pool
    except Exception:
        await pool.aclose()
    if preferred and same_chain:
        pool = RpcPool(same_chain, chain.chain_id, timeout_ms=4000)
        try:
            await pool.probe()
            return pool
        except Exception:
            pool.primary_url = same_chain[0]
            pool.broadcast_urls = same_chain
            pool.healthy_urls = same_chain
            return pool
    pool = RpcPool(urls, chain.chain_id, timeout_ms=4000)
    pool.primary_url = urls[0]
    pool.broadcast_urls = urls
    pool.healthy_urls = urls
    return pool


async def _eth_call(pool: RpcPool, contract: str, data: str) -> str:
    return await pool.eth_call({"to": contract, "data": data})


async def _supports(pool: RpcPool, contract: str, iid: str) -> bool:
    try:
        raw = bytes.fromhex(iid[2:] if iid.startswith("0x") else iid)
        data = encode_call("supportsInterface(bytes4)", ["bytes4"], [raw])
        out = await _eth_call(pool, contract, data)
        return bool(decode_uint(out))
    except Exception:
        return False


async def detect_standard(pool: RpcPool, contract: str, hint: str) -> str:
    if hint in {"erc721", "erc1155"}:
        return hint
    if await _supports(pool, contract, ERC721_IID):
        return "erc721"
    if await _supports(pool, contract, ERC1155_IID):
        return "erc1155"
    return "erc721"


async def _balance_721(pool: RpcPool, contract: str, owner: str) -> int:
    data = encode_call("balanceOf(address)", ["address"], [checksum(owner)])
    out = await _eth_call(pool, contract, data)
    return int(decode_uint(out) or 0)


async def _owner_of(pool: RpcPool, contract: str, token_id: int) -> str | None:
    data = encode_call("ownerOf(uint256)", ["uint256"], [token_id])
    out = await _eth_call(pool, contract, data)
    return decode_address(out)


async def _balance_1155(pool: RpcPool, contract: str, owner: str, token_id: int) -> int:
    data = encode_call("balanceOf(address,uint256)", ["address", "uint256"], [checksum(owner), token_id])
    out = await _eth_call(pool, contract, data)
    return int(decode_uint(out) or 0)


async def _token_of_owner(pool: RpcPool, contract: str, owner: str, index: int) -> int | None:
    data = encode_call(
        "tokenOfOwnerByIndex(address,uint256)",
        ["address", "uint256"],
        [checksum(owner), index],
    )
    out = await _eth_call(pool, contract, data)
    value = decode_uint(out)
    return None if value is None else int(value)


def _token_from_721_log(log: dict[str, Any]) -> int | None:
    topics = log.get("topics") or []
    if len(topics) >= 4:
        return int(topics[3], 16)
    data = log.get("data") or "0x"
    if data not in {"0x", "0x0", None}:
        try:
            return int(data[:66], 16)
        except Exception:
            return None
    return None


def _range_too_large(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "block range is too large",
            "query returned more than",
            "query timeout",
            "range limit",
            "-32062",
            "exceeds the max",
            "too many blocks",
            "eth_getlogs is limited",
            "response size exceeded",
            "log response size exceeded",
        )
    )


async def _get_logs(pool: RpcPool, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = await pool.call("eth_getLogs", [params])
    return result or []


async def _get_logs_range(
    pool: RpcPool,
    contract: str,
    topics: list[str | None],
) -> list[dict[str, Any]]:
    latest = await pool.get_block_number()
    start = max(0, latest - LOG_LOOKBACK)
    span = LOG_CHUNK
    logs: list[dict[str, Any]] = []
    cursor = start
    while cursor <= latest:
        end = min(latest, cursor + span - 1)
        try:
            batch = await _get_logs(
                pool,
                {
                    "address": contract,
                    "fromBlock": hex(cursor),
                    "toBlock": hex(end),
                    "topics": topics,
                },
            )
            logs.extend(batch)
            cursor = end + 1
        except Exception as exc:
            if _range_too_large(exc) and span > LOG_CHUNK_MIN:
                span = max(LOG_CHUNK_MIN, span // 2)
                continue
            raise
    return logs


async def _scan_721_logs(pool: RpcPool, contract: str, owner: str) -> list[int]:
    owner_topic = _topic_address(owner)
    incoming = await _get_logs_range(pool, contract, [TRANSFER_721, None, owner_topic])
    outgoing = await _get_logs_range(pool, contract, [TRANSFER_721, owner_topic, None])
    held: dict[int, int] = {}
    for log in incoming:
        token_id = _token_from_721_log(log)
        if token_id is not None:
            held[token_id] = held.get(token_id, 0) + 1
    for log in outgoing:
        token_id = _token_from_721_log(log)
        if token_id is not None:
            held[token_id] = held.get(token_id, 0) - 1
    return sorted(token_id for token_id, count in held.items() if count > 0)


async def _tokens_of_owner_list(pool: RpcPool, contract: str, owner: str) -> list[int]:
    for signature in ("tokensOfOwner(address)", "walletOfOwner(address)"):
        try:
            data = encode_call(signature, ["address"], [checksum(owner)])
            out = await _eth_call(pool, contract, data)
            decoded = decode_data(["uint256[]"], out)
            if decoded and decoded[0] is not None:
                return [int(item) for item in decoded[0]]
        except Exception:
            continue
    return []


def _row_contract(row: dict[str, Any]) -> str:
    token = row.get("token") if isinstance(row.get("token"), dict) else {}
    for key in (
        "TokenAddress",
        "tokenAddress",
        "contractAddress",
        "ContractAddress",
        "address",
        "address_hash",
    ):
        value = row.get(key) or token.get(key)
        if value:
            return str(value).lower()
    return ""


def _row_token_id(row: dict[str, Any]) -> int | None:
    for key in ("TokenId", "tokenID", "tokenId", "token_id", "id"):
        value = row.get(key)
        if value is None:
            continue
        try:
            return _as_int(value)
        except Exception:
            continue
    return None


async def _scan_etherscan_nfts(chain: ChainPreset, contract: str, owner: str) -> list[int]:
    if not chain.explorer_api or not chain.etherscan_chain_id:
        return []
    key = get_settings().etherscan_api_key
    want = contract.lower()
    found: list[int] = []
    async with httpx.AsyncClient(timeout=20, headers=EXPLORER_HEADERS) as client:
        for page in range(1, 6):
            params = {
                "chainid": chain.etherscan_chain_id,
                "module": "account",
                "action": "addnft",
                "address": checksum(owner),
                "contractaddress": checksum(contract),
                "page": page,
                "offset": 100,
            }
            if key:
                params["apikey"] = key
            response = await client.get(chain.explorer_api, params=params)
            if response.status_code >= 400:
                break
            body = response.json()
            rows = body.get("result")
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                addr = _row_contract(row)
                if addr and addr != want:
                    continue
                token_id = _row_token_id(row)
                if token_id is not None:
                    found.append(token_id)
            if len(rows) < 100 or len(found) >= 256:
                break
    return found


async def _scan_blockscout(chain: ChainPreset, contract: str, owner: str) -> list[int]:
    bases: list[str] = []
    if chain.explorer:
        bases.append(chain.explorer.rstrip("/") + "/api/v2")
    if chain.explorer_api:
        api = chain.explorer_api.rstrip("/")
        bases.append(api + "/v2" if api.endswith("/api") else api)
    seen: set[str] = set()
    want = contract.lower()
    found: list[int] = []
    async with httpx.AsyncClient(timeout=20, headers=EXPLORER_HEADERS) as client:
        for base in bases:
            if base in seen:
                continue
            seen.add(base)
            url = f"{base}/addresses/{checksum(owner)}/nft"
            params: dict[str, str] = {"type": "ERC-721"}
            ok = False
            for _ in range(8):
                response = await client.get(url, params=params)
                if response.status_code >= 400:
                    break
                body = response.json()
                ok = True
                for item in body.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    addr = _row_contract(item)
                    if addr and addr != want:
                        continue
                    token_id = _row_token_id(item)
                    if token_id is not None:
                        found.append(token_id)
                nxt = body.get("next_page_params")
                if not nxt:
                    break
                params = {str(k): str(v) for k, v in nxt.items()}
                params.setdefault("type", "ERC-721")
            if ok:
                break
    return found


async def _scan_explorer(chain: ChainPreset, contract: str, owner: str) -> list[int]:
    blob = f"{chain.explorer or ''} {chain.explorer_api or ''}".lower()
    if chain.etherscan_chain_id and chain.explorer_api and "etherscan.io" in blob:
        found = await _scan_etherscan_nfts(chain, contract, owner)
        if found:
            return found
    if "blockscout" in blob:
        return await _scan_blockscout(chain, contract, owner)
    return []


async def _scan_721(
    pool: RpcPool,
    contract: str,
    owner: str,
    chain: ChainPreset,
) -> tuple[list[int], str]:
    try:
        balance = await _balance_721(pool, contract, owner)
    except Exception as exc:
        return [], f"balanceOf 失败：{exc}"
    if balance <= 0:
        return [], "这个钱包在该合约下余额为 0"
    owned = await _tokens_of_owner_list(pool, contract, owner)
    if owned:
        return owned[:256], ""
    enumerable = await _supports(pool, contract, ERC721_ENUMERABLE_IID)
    if enumerable:
        tokens: list[int] = []
        try:
            for index in range(min(balance, 256)):
                token_id = await _token_of_owner(pool, contract, owner, index)
                if token_id is not None:
                    tokens.append(token_id)
            if tokens:
                return tokens, ""
        except Exception:
            pass
    explorer_ids = await _scan_explorer(chain, contract, owner)
    if explorer_ids:
        return explorer_ids[:256], ""
    try:
        tokens = await _scan_721_logs(pool, contract, owner)
        if tokens:
            return tokens, ""
        return [], f"链上余额 {balance}，但扫不到 token ID。请改用手动 ID，或填 OpenSea 上看到的编号。"
    except Exception as exc:
        return [], f"扫描失败：{exc}"


async def _owned_721(pool: RpcPool, contract: str, owner: str, token_ids: list[int]) -> list[int]:
    owned: list[int] = []
    for token_id in token_ids:
        try:
            current = await _owner_of(pool, contract, token_id)
        except Exception:
            continue
        if current and current.lower() == owner.lower():
            owned.append(token_id)
    return owned


async def _owned_1155(pool: RpcPool, contract: str, owner: str, token_ids: list[int]) -> list[tuple[int, int]]:
    held: list[tuple[int, int]] = []
    for token_id in token_ids:
        try:
            amount = await _balance_1155(pool, contract, owner, token_id)
        except Exception:
            continue
        if amount > 0:
            held.append((token_id, amount))
    return held


async def _resolve_wallet_tokens(
    pool: RpcPool,
    request: SweepRequest,
    wallet: ResolvedWallet,
    standard: str,
    contract: str,
) -> tuple[list[tuple[int, int]], str]:
    if standard == "erc1155":
        if request.mode == "scan":
            return [], "ERC-1155 请用本次结果或手动 ID"
        raw_ids = [_as_int(x) for x in request.token_ids] if request.mode == "manual" else _ids_from_map(request.last_mint, wallet.address)
        held = await _owned_1155(pool, contract, wallet.address, raw_ids)
        if request.max_per_wallet:
            held = held[: request.max_per_wallet]
        return held, "" if held else "没有可转 NFT"
    try:
        chain = get_chain(request.chain_id)
    except ValueError as exc:
        return [], str(exc)
    if request.mode == "manual":
        tokens = await _owned_721(pool, contract, wallet.address, [_as_int(x) for x in request.token_ids])
        note = ""
    elif request.mode == "last_mint" and _ids_from_map(request.last_mint, wallet.address):
        tokens = await _owned_721(pool, contract, wallet.address, _ids_from_map(request.last_mint, wallet.address))
        note = ""
    else:
        tokens, note = await _scan_721(pool, contract, wallet.address, chain)
        if request.mode == "last_mint" and tokens:
            note = "本次结果为空，已改扫链上持仓"
    if request.max_per_wallet:
        tokens = tokens[: request.max_per_wallet]
    if not tokens:
        return [], note or "没有可转 NFT"
    return [(token_id, 1) for token_id in tokens], note


def _transfer_data(standard: str, sender: str, dest: str, token_id: int, amount: int) -> str:
    if standard == "erc1155":
        return encode_call(
            "safeTransferFrom(address,address,uint256,uint256,bytes)",
            ["address", "address", "uint256", "uint256", "bytes"],
            [checksum(sender), checksum(dest), token_id, amount, b""],
        )
    return encode_call(
        "safeTransferFrom(address,address,uint256)",
        ["address", "address", "uint256"],
        [checksum(sender), checksum(dest), token_id],
    )


def _fallback_transfer_data(sender: str, dest: str, token_id: int) -> str:
    return encode_call(
        "transferFrom(address,address,uint256)",
        ["address", "address", "uint256"],
        [checksum(sender), checksum(dest), token_id],
    )


async def preview_sweep(request: SweepRequest) -> dict[str, Any]:
    if not is_address(request.contract):
        raise ConfigError("NFT 合约地址格式不对")
    if request.mode not in {"last_mint", "scan", "manual"}:
        raise ConfigError("unknown sweep mode")
    if request.mode == "manual" and not request.token_ids:
        raise ConfigError("手动模式请填写 token ID")
    if not request.wallets:
        raise ConfigError("归集需要源钱包私钥")
    try:
        chain = get_chain(request.chain_id)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    dest = checksum(request.destination) if request.destination and is_address(request.destination) else None
    managers = WalletManager(request.wallets)
    signers = managers.signers()
    if not signers:
        raise ConfigError("归集需要源钱包私钥")
    pool = await _open_pool(request.chain_id, request.rpc_urls)
    events: list[str] = []
    try:
        contract = checksum(request.contract)
        code = await pool.get_code(contract)
        if not code or code in {"0x", "0x0"}:
            raise ConfigError("该地址没有合约代码")
        standard = await detect_standard(pool, contract, request.standard)
        rows = []
        for wallet in signers:
            tokens, note = await _resolve_wallet_tokens(pool, request, wallet, standard, contract)
            status = "READY" if tokens else "SKIP"
            rows.append(
                {
                    "label": wallet.label,
                    "address": wallet.address,
                    "token_ids": [str(token_id) for token_id, _ in tokens],
                    "amounts": [amount for _, amount in tokens],
                    "status": status,
                    "note": note,
                }
            )
            events.append(f"{wallet.label} {status} ids={','.join(str(t) for t, _ in tokens) or '-'}")
        dest_is_source = bool(dest and any(w.address.lower() == dest.lower() for w in signers))
        return {
            "chain_id": chain.chain_id,
            "chain_name": chain.name,
            "contract": contract,
            "destination": dest,
            "dest_is_source": dest_is_source,
            "mode": request.mode,
            "standard": standard,
            "wallets": rows,
            "ready": sum(1 for row in rows if row["status"] == "READY"),
            "skipped": sum(1 for row in rows if row["status"] == "SKIP"),
            "events": events,
        }
    finally:
        await pool.aclose()


async def _send_one(
    pool: RpcPool,
    request: SweepRequest,
    wallet: ResolvedWallet,
    dest: str,
    contract: str,
    standard: str,
    token_id: int,
    amount: int,
    nonce: int,
) -> dict[str, Any]:
    data = _transfer_data(standard, wallet.address, dest, token_id, amount)
    fallback = request.gas.fallback_gas_limit if request.gas.fallback_gas_limit != 280000 else 120000
    retries = request.gas.max_retries
    last: dict[str, Any] | None = None
    for attempt in range(retries + 1):
        quote = await quote_gas(pool, request.gas, fallback_limit=fallback, attempt=attempt)
        tx = {
            "from": wallet.address,
            "to": contract,
            "data": data,
            "value": "0x0",
        }
        try:
            quote["gas_limit"], _source = await resolve_gas_limit(pool, tx, request.gas)
        except Exception:
            if standard != "erc1155":
                data = _fallback_transfer_data(wallet.address, dest, token_id)
                tx["data"] = data
                quote["gas_limit"], _source = await resolve_gas_limit(pool, tx, request.gas)
        plan = TxPlan(
            to=contract,
            data=data,
            value=0,
            gas=quote["gas_limit"],
            max_fee_per_gas=quote["max_fee"],
            max_priority_fee_per_gas=quote["priority_fee"],
            nonce=nonce,
            chain_id=request.chain_id,
            from_address=wallet.address,
        )
        raw, tx_hash = sign_tx(plan, wallet.private_key or "")
        broadcasts = await pool.broadcast(raw)
        sent = [row for row in broadcasts if row.get("ok")]
        if not sent:
            last = {
                "label": wallet.label,
                "address": wallet.address,
                "token_id": str(token_id),
                "amount": amount,
                "tx_hash": tx_hash,
                "status": "SEND_FAILED",
                "error": "all RPC broadcasts failed",
            }
            continue
        receipt = await pool.wait_receipt(tx_hash, timeout=request.gas.receipt_timeout_sec)
        if receipt is None:
            late = await pool.wait_receipt(tx_hash, timeout=10)
            receipt = late
        if receipt is None:
            last = {
                "label": wallet.label,
                "address": wallet.address,
                "token_id": str(token_id),
                "amount": amount,
                "tx_hash": tx_hash,
                "status": "TIMEOUT",
                "error": "receipt timeout",
            }
            continue
        ok = int(receipt.get("status") or "0x0", 16) == 1
        return {
            "label": wallet.label,
            "address": wallet.address,
            "token_id": str(token_id),
            "amount": amount,
            "tx_hash": tx_hash,
            "status": "SUCCESS" if ok else "REVERTED",
            "error": None if ok else "execution reverted",
        }
    return last or {
        "label": wallet.label,
        "address": wallet.address,
        "token_id": str(token_id),
        "amount": amount,
        "tx_hash": None,
        "status": "SEND_FAILED",
        "error": "retry exhausted",
    }


async def run_sweep(request: SweepRequest) -> dict[str, Any]:
    if not request.destination or not is_address(request.destination):
        raise ConfigError("归集目标必须是 0x 开头的 40 位地址")
    preview = await preview_sweep(request)
    dest = checksum(request.destination)
    managers = WalletManager(request.wallets)
    by_address = {w.address.lower(): w for w in managers.signers()}
    pool = await _open_pool(request.chain_id, request.rpc_urls)
    events = list(preview.get("events") or [])
    results: list[dict[str, Any]] = []
    try:
        sem = asyncio.Semaphore(max(1, request.concurrency))

        async def one_wallet(row: dict[str, Any]) -> list[dict[str, Any]]:
            if row["status"] != "READY":
                return []
            wallet = by_address.get((row["address"] or "").lower())
            if not wallet or not wallet.private_key:
                return [{"label": row["label"], "address": row["address"], "status": "SKIP", "error": "no key"}]
            async with sem:
                nonce = await pool.get_nonce(wallet.address, "pending")
                wallet_results = []
                pairs = list(zip(row["token_ids"], row["amounts"] or [1] * len(row["token_ids"])))
                for token_id, amount in pairs:
                    item = await _send_one(
                        pool,
                        request,
                        wallet,
                        dest,
                        preview["contract"],
                        preview["standard"],
                        _as_int(token_id),
                        int(amount or 1),
                        nonce,
                    )
                    wallet_results.append(item)
                    events.append(f"{wallet.label} #{token_id} {item['status']} {item.get('tx_hash') or ''}")
                    if item["status"] in {"SUCCESS", "REVERTED"}:
                        nonce += 1
                return wallet_results

        batches = await asyncio.gather(*(one_wallet(row) for row in preview["wallets"]))
        for batch in batches:
            results.extend(batch)
        return {
            **preview,
            "destination": dest,
            "results": results,
            "success": sum(1 for row in results if row.get("status") == "SUCCESS"),
            "failed": sum(1 for row in results if row.get("status") not in {"SUCCESS", "SKIP"}),
            "events": events,
        }
    finally:
        await pool.aclose()
