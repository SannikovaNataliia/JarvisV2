"""The single shared microphone input stream.

Exactly one sd.RawInputStream exists in the process, opened once when voice
mode starts. Consumers call subscribe() to get an asyncio.Queue of raw PCM
chunks fed from a background PortAudio thread; adding or removing a
subscriber never touches the stream itself. A consumer that needs different
framing than the stream's own blocksize (e.g. OpenWakeWord's 1280-sample
frames in step 5c) re-buffers on its own side — this module's chunk size
never changes to suit a particular consumer.
"""

import asyncio
import logging
import threading
from typing import List, Optional

import sounddevice as sd

from backend import config

logger = logging.getLogger(__name__)

_stream: Optional[sd.RawInputStream] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_subscribers: List["asyncio.Queue[bytes]"] = []
_lock = threading.Lock()

_OPEN_RETRIES = 5
_RETRY_DELAY_S = 0.5


def _find_device() -> Optional[int]:
    try:
        devices = sd.query_devices()
    except Exception:
        logger.exception("mic: failed to query audio devices")
        return None
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0 and config.MIC_NAME_HINT.lower() in dev["name"].lower():
            logger.info("mic: using input device %r (index %d)", dev["name"], idx)
            return idx
    logger.warning("mic: no device matching %r, falling back to system default input", config.MIC_NAME_HINT)
    return None


def _deliver(q: "asyncio.Queue[bytes]", chunk: bytes) -> None:
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(chunk)


def _callback(indata, frames, time_info, status) -> None:
    if status:
        logger.warning("mic: stream status: %s", status)
    chunk = bytes(indata)
    loop = _loop
    if loop is None or loop.is_closed():
        return
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            loop.call_soon_threadsafe(_deliver, q, chunk)
        except RuntimeError:
            pass  # loop closed between the check above and this call


def _open_stream(device: Optional[int]) -> sd.RawInputStream:
    stream = sd.RawInputStream(
        samplerate=config.SAMPLE_RATE_IN,
        channels=1,
        dtype="int16",
        blocksize=config.CHUNK_SIZE,
        device=device,
        callback=_callback,
    )
    stream.start()
    return stream


async def start() -> None:
    """Idempotent: a second call while the stream is open is a no-op."""
    global _stream, _loop
    if _stream is not None:
        return
    _loop = asyncio.get_running_loop()
    device = _find_device()
    last_err: Optional[OSError] = None
    for attempt in range(1, _OPEN_RETRIES + 1):
        try:
            _stream = await asyncio.to_thread(_open_stream, device)
            logger.info("mic: stream started (%d Hz, blocksize=%d)", config.SAMPLE_RATE_IN, config.CHUNK_SIZE)
            return
        except OSError as e:
            last_err = e
            logger.warning("mic: failed to open stream (attempt %d/%d): %s", attempt, _OPEN_RETRIES, e)
            await asyncio.sleep(_RETRY_DELAY_S)
    _loop = None
    raise RuntimeError(f"mic: could not open input stream after {_OPEN_RETRIES} attempts: {last_err}")


def stop() -> None:
    """Closes the stream and releases the device. Safe to call when already stopped."""
    global _stream, _loop
    if _stream is None:
        return
    try:
        _stream.stop()
        _stream.close()
    finally:
        _stream = None
        _loop = None
    logger.info("mic: stream stopped")


def subscribe() -> "asyncio.Queue[bytes]":
    q: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=50)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "asyncio.Queue[bytes]") -> None:
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)
