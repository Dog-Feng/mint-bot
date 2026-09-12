import uvicorn

from mint_engine.config.settings import get_settings

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
