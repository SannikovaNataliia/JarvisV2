"""Thread-safe log bus.

Producers: any thread — stdlib `logging` calls, raw stdout/stderr writes from
third-party code (PyAudio, pywinauto, tracebacks). Consumers: callbacks
registered via `bus.subscribe()`, e.g. the WebSocket server forwarding `log`
messages to connected clients.
"""

import logging
import re
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Callable, List

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class LogRecord:
    level: str  # debug | info | warn | error
    source: str  # logger name, or "stdout" / "stderr"
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


class LogBus:
    def __init__(self) -> None:
        self._subscribers: List[Callable[[LogRecord], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[LogRecord], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[LogRecord], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def publish(self, level: str, source: str, text: str) -> None:
        if not text or not text.strip():
            return
        record = LogRecord(level=level, source=source, text=text)
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(record)
            except Exception:
                pass  # a broken subscriber must never take down the bus


bus = LogBus()

_LEVELNO_TO_NAME = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


class LogBusHandler(logging.Handler):
    """logging.Handler that pushes formatted records onto the bus."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = _LEVELNO_TO_NAME.get(record.levelno, "info")
            bus.publish(level, record.name, _ANSI_RE.sub("", self.format(record)))
        except Exception:
            pass


class _TeeStream:
    """Writes through to the real stream, then mirrors non-empty writes onto the bus.

    A per-instance guard stops a subscriber's own print/log call (triggered
    while handling a published record) from re-entering this same write()
    and recursing.
    """

    def __init__(self, real_stream, source: str, level: str) -> None:
        self._real = real_stream
        self._source = source
        self._level = level
        self._in_write = False

    def write(self, text: str) -> int:
        n = self._real.write(text)  # real terminal keeps colors; only the bus copy is stripped
        if not self._in_write and text.strip():
            self._in_write = True
            try:
                clean = _ANSI_RE.sub("", text).rstrip("\n")
                bus.publish(self._level, self._source, clean)
            finally:
                self._in_write = False
        return n

    def flush(self) -> None:
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


# Passed as log_config= to uvicorn.run()/uvicorn.Config(). Strips uvicorn's own
# colorized "default"/"access" StreamHandlers and lets its loggers propagate to
# root instead, where setup_logging() has already attached the bus handler —
# so uvicorn's INFO/access lines arrive with their real level instead of being
# picked up as raw text through the stderr tee (mislabeled "error", with ANSI
# codes intact).
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": False,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "use_colors": False,
        },
    },
    "handlers": {},
    "loggers": {
        "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
    },
}


_installed = False


def setup_logging(level: int = logging.DEBUG) -> None:
    """Wires stdlib logging and stdout/stderr into the bus. Idempotent.

    Order matters: the console handler is bound to the *real* stderr before
    the tee replaces sys.stderr, so terminal output is never doubled and
    logging calls never loop back through the tee.
    """
    global _installed
    if _installed:
        return
    _installed = True

    real_stdout, real_stderr = sys.stdout, sys.stderr

    console_handler = logging.StreamHandler(real_stderr)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )

    bus_handler = LogBusHandler()
    bus_handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(bus_handler)

    # These libraries log every frame/request at DEBUG, including raw payload
    # bytes (e.g. websockets logs the full JSON of every session.send() call,
    # base64 audio and all). At root=DEBUG that flooded the bus badly enough
    # to visibly delay transcript delivery over the WebSocket — not cosmetic.
    for noisy in ("websockets", "websockets.client", "websockets.server", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # stdout -> info, stderr -> error, so the console panel's level filter is meaningful.
    sys.stdout = _TeeStream(real_stdout, "stdout", "info")
    sys.stderr = _TeeStream(real_stderr, "stderr", "error")
