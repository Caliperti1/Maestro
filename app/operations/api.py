"""Production ASGI process entry point.

Keeping the Uvicorn invocation in Python avoids shell interpolation for Render's ``PORT`` value
and makes the same image usable outside Render.
"""

import os

import uvicorn

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    port = int(os.environ.get("PORT", settings.app_port))
    uvicorn.run(
        "app.api.main:app",
        host=settings.app_host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
