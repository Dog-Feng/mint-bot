import logging

import uvicorn

from mint_engine.config.settings import get_settings


class _ComponentTag(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("mint_engine.treasury"):
            record.component = "treasury"
        elif record.name.startswith("mint_engine.app"):
            record.component = "app"
        else:
            record.component = "mint"
        return True


def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, force=True)
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)

    mint = logging.getLogger("mint_engine")
    mint.setLevel(logging.INFO)
    mint.propagate = False
    logging.getLogger("mint_engine.treasury").setLevel(logging.INFO)
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


_configure_logging()

settings = get_settings()
print(f"Mint Engine listening on http://{settings.host}:{settings.port} (no auth)")
uvicorn.run(
    "mint_engine.app:app",
    host=settings.host,
    port=settings.port,
    reload=False,
    proxy_headers=True,
    forwarded_allow_ips="*",
    access_log=False,
)
