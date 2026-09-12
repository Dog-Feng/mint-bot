from __future__ import annotations


class EngineError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class ConfigError(EngineError):
    def __init__(self, message: str, **kwargs):
        super().__init__("CONFIG_ERROR", message, **kwargs)


class RpcError(EngineError):
    def __init__(self, message: str, **kwargs):
        super().__init__("RPC_ERROR", message, retryable=True, **kwargs)
