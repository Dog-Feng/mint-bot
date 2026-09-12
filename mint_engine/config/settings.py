from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    etherscan_api_key: str = ""
    opensea_api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8056
    abi_cache_dir: Path = ROOT / "abi_cache"
    results_dir: Path = ROOT / "results"


@lru_cache
def get_settings() -> Settings:
    return Settings()
