import logging

import uvicorn

from mint_engine.config.settings import get_settings


def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, force=True)
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)

    mint = logging.getLogger("mint_engine")
    mint.setLevel(logging.INFO)
    mint.propagate = False
    if not mint.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [mint] %(message)s",
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
)
