"""Start Forgeyard: `uv run python main.py` (migrations run on boot)."""

import logging

import uvicorn

from forgeyard.app import create_app
from forgeyard.config import get_settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    uvicorn.run(create_app(), host=settings.host, port=settings.port, proxy_headers=False)
