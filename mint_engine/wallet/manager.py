from __future__ import annotations

from eth_account import Account

from mint_engine.core.exceptions import ConfigError
from mint_engine.core.models import WalletItem
from mint_engine.evm import checksum, is_address


class ResolvedWallet:
    def __init__(self, label: str, address: str, private_key: str | None):
        self.label = label
        self.address = address
        self.private_key = private_key

    @property
    def can_sign(self) -> bool:
        return bool(self.private_key)


class WalletManager:
    def __init__(self, items: list[WalletItem]):
        self.wallets = [self._resolve(item, idx) for idx, item in enumerate(items)]

    def _resolve(self, item: WalletItem, idx: int) -> ResolvedWallet:
        label = item.label or f"wallet_{idx + 1}"
        if item.private_key:
            key = item.private_key
            if not key.startswith("0x"):
                key = "0x" + key
            try:
                account = Account.from_key(key)
            except Exception as exc:
                raise ConfigError(f"{label}: invalid private key") from exc
            return ResolvedWallet(label, checksum(account.address), key)
        if item.address and is_address(item.address):
            return ResolvedWallet(label, checksum(item.address), None)
        raise ConfigError(f"{label}: need a private key or a valid address")

    def signers(self) -> list[ResolvedWallet]:
        return [w for w in self.wallets if w.can_sign]
