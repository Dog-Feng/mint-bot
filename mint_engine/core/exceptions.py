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


class RunCancelled(Exception):
    """User or heartbeat cancelled the active mint run."""

    def __init__(self, message: str = "run cancelled") -> None:
        super().__init__(message)
        self.message = message


class RpcError(EngineError):
    def __init__(self, message: str, **kwargs):
        super().__init__("RPC_ERROR", message, retryable=True, **kwargs)


def is_rate_limited(exc: Exception) -> bool:
    if isinstance(exc, EngineError) and exc.details.get("rate_limited"):
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text or "exceeded its compute units" in text


def is_execution_reverted(exc: Exception) -> bool:
    if is_rate_limited(exc):
        return False
    if isinstance(exc, EngineError):
        code = exc.details.get("code")
        if code in (3, "3", -32015, "-32015"):
            return True
        data = exc.details.get("data")
        if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
            return True
        blob = " ".join(
            str(part)
            for part in (exc.message, exc.details.get("message"), data)
            if part
        ).lower()
    else:
        blob = str(exc).lower()
    return any(
        marker in blob
        for marker in (
            "execution reverted",
            "vm execution",
            "invalid opcode",
            "out of gas",
        )
    )
