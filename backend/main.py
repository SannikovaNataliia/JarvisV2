"""Entry point: python -m backend.main. Runs standalone, no frontend or shell required."""

import logging

import uvicorn

from backend import config
from backend.logbus import UVICORN_LOG_CONFIG, setup_logging
from backend.server import app

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging()  # must run before uvicorn.run() so root already has the bus handler
    logger.info("Starting Jarvis backend on %s:%s", config.HOST, config.PORT)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=UVICORN_LOG_CONFIG, use_colors=False)


if __name__ == "__main__":
    main()
