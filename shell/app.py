"""Minimal pywebview launcher: starts the backend in-process and opens a native
window pointing at the served frontend. No browser chrome, no address bar.
"""

import logging
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
import webview

from backend import config
from backend.logbus import UVICORN_LOG_CONFIG, setup_logging
from backend.server import app

logger = logging.getLogger(__name__)


def _wait_for_server(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> None:
    setup_logging()  # must run before uvicorn.Config() so root already has the bus handler

    uv_config = uvicorn.Config(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        log_config=UVICORN_LOG_CONFIG,
        use_colors=False,
    )
    server = uvicorn.Server(uv_config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(config.HOST, config.PORT):
        logger.error("Backend did not start in time, aborting shell launch")
        return

    window = webview.create_window(
        "Jarvis",
        f"http://{config.HOST}:{config.PORT}/",
        width=1000,
        height=700,
        resizable=True,
    )

    def on_closed():
        logger.info("Window closed, shutting down backend")
        server.should_exit = True

    window.events.closed += on_closed

    webview.start()
    server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
