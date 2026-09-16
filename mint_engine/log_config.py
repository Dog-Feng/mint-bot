from __future__ import annotations

import logging


class _ComponentTag(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("mint_engine.treasury"):
            record.component = "treasury"
        elif record.name.startswith("mint_engine.app"):
            record.component = "app"
        else:
            record.component = "mint"
        return True


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    logging.basicConfig(level=logging.WARNING, force=True)
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)

    mint = logging.getLogger("mint_engine")
    mint.setLevel(logging.INFO)
    mint.propagate = False
    logging.getLogger("mint_engine.treasury").setLevel(logging.INFO)
    logging.getLogger("mint_engine.app").setLevel(logging.INFO)
    if not mint.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(_ComponentTag())
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(component)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        mint.addHandler(handler)
