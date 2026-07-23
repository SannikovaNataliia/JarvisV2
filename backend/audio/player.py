"""Backend-only audio playback. The frontend never touches audio — this is
the only place output PCM is written to a device.
"""

import asyncio
import logging
from typing import Optional

import sounddevice as sd

from backend import config

logger = logging.getLogger(__name__)

_stream: Optional[sd.RawOutputStream] = None
_queue: "Optional[asyncio.Queue[Optional[bytes]]]" = None
_writer_task: Optional[asyncio.Task] = None
_playing = False


def is_playing() -> bool:
    return _playing


async def start() -> None:
    """Idempotent: a second call while the stream is open is a no-op."""
    global _stream, _queue, _writer_task
    if _stream is not None:
        return
    _stream = sd.RawOutputStream(samplerate=config.SAMPLE_RATE_OUT, channels=1, dtype="int16")
    _stream.start()
    _queue = asyncio.Queue()
    _writer_task = asyncio.create_task(_writer_loop())
    logger.info("player: stream started (%d Hz)", config.SAMPLE_RATE_OUT)


async def _writer_loop() -> None:
    global _playing
    assert _queue is not None and _stream is not None
    try:
        while True:
            chunk = await _queue.get()
            if chunk is None:  # shutdown sentinel from stop()
                break
            _playing = True
            try:
                await asyncio.to_thread(_stream.write, chunk)
            except Exception:
                logger.exception("player: write failed")
            _playing = not _queue.empty()
    except asyncio.CancelledError:
        pass


def play(chunk: bytes) -> None:
    if _queue is None:
        return
    _queue.put_nowait(chunk)


def flush() -> None:
    """Drops everything queued and not-yet-written — needed for barge-in."""
    global _playing
    if _queue is None:
        return
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    _playing = False


async def stop() -> None:
    global _stream, _queue, _writer_task, _playing
    if _stream is None:
        return
    flush()
    _queue.put_nowait(None)
    if _writer_task is not None:
        await _writer_task
        _writer_task = None
    _stream.stop()
    _stream.close()
    _stream = None
    _queue = None
    _playing = False
    logger.info("player: stream stopped")
