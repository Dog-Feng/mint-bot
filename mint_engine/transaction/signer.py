from __future__ import annotations

from eth_account import Account

from mint_engine.core.models import TxPlan


def sign_tx(plan: TxPlan, private_key: str) -> tuple[str, str]:
    payload = {
        "to": plan.to,
        "data": plan.data,
        "value": plan.value,
        "gas": plan.gas,
        "nonce": plan.nonce,
        "chainId": plan.chain_id,
        "type": 2,
        "maxFeePerGas": plan.max_fee_per_gas,
        "maxPriorityFeePerGas": plan.max_priority_fee_per_gas,
    }
    signed = Account.sign_transaction(payload, private_key)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    return raw, signed.hash.hex() if signed.hash.hex().startswith("0x") else "0x" + signed.hash.hex()
