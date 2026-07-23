"""FastAPI app with a WebSocket endpoint at /ws, bound to 127.0.0.1 only.

Runs fine with zero clients connected — outbound messages are simply dropped
if there is no one to send them to.
"""

import asyncio
import logging
from typing import Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from backend import config, protocol
from backend.facade import JarvisBackend, State
from backend.live_session import check_model_availability
from backend.logbus import LogRecord, bus

logger = logging.getLogger(__name__)

app = FastAPI()
backend = JarvisBackend()

_clients: Set[WebSocket] = set()
_main_loop: Optional[asyncio.AbstractEventLoop] = None


async def _broadcast(msg_type: str, data: dict) -> None:
    if not _clients:
        return
    payload = protocol.dumps(msg_type, data)
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


async def _broadcast_state() -> None:
    await _broadcast(
        protocol.STATE,
        {"state": backend.state.value, "mode": backend.mode.value, "model": config.GEMINI_TEXT_MODEL},
    )


async def _on_state(_state: State) -> None:
    await _broadcast_state()


async def _on_transcript(role: str, text: str, final: bool) -> None:
    await _broadcast(protocol.TRANSCRIPT, {"role": role, "text": text, "final": final})


async def _on_error(message: str, fatal: bool) -> None:
    await _broadcast(protocol.ERROR, {"message": message, "fatal": fatal})


backend.on_state(_on_state)
backend.on_transcript(_on_transcript)
backend.on_error(_on_error)


def _on_log_record(record: LogRecord) -> None:
    loop = _main_loop
    if loop is None or loop.is_closed():
        return

    def _schedule() -> None:
        # Runs inside the loop thread; the loop can still close between
        # call_soon_threadsafe below and this callback actually running.
        if not loop.is_closed():
            asyncio.create_task(_broadcast(protocol.LOG, record.to_dict()))

    try:
        loop.call_soon_threadsafe(_schedule)
    except RuntimeError:
        pass  # loop closed between the check above and this call


@app.on_event("startup")
async def _startup() -> None:
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    bus.subscribe(_on_log_record)
    await check_model_availability()  # once per process, not per voice-mode entry
    logger.info("Jarvis backend ready on %s:%s", config.HOST, config.PORT)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _clients.add(websocket)
    logger.debug("client connected (%d total)", len(_clients))
    try:
        await websocket.send_text(protocol.dumps(protocol.READY, backend.get_state()))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = protocol.parse(raw)
            except protocol.ProtocolError as e:
                await websocket.send_text(protocol.dumps(protocol.ERROR, {"message": str(e), "fatal": False}))
                continue
            await _handle_message(msg)
    except WebSocketDisconnect:
        pass
    except RuntimeError as e:
        # A client can disconnect while we're mid-await inside _handle_message
        # (e.g. set_mode("voice") opening the mic + Live socket takes real
        # time) — Starlette then raises a bare RuntimeError from receive_text()
        # instead of WebSocketDisconnect. Treat it the same: log and clean up,
        # never let a disconnect race take down the connection handler.
        logger.debug("client connection closed mid-request: %s", e)
    finally:
        _clients.discard(websocket)
        logger.debug("client disconnected (%d total)", len(_clients))


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


async def _handle_message(msg: dict) -> None:
    mtype = msg["type"]
    data = msg.get("data", {})

    if mtype == protocol.SEND_TEXT:
        await backend.send_text(data.get("text", ""))
    elif mtype == protocol.SET_MODE:
        requested = data.get("mode", "text")
        logger.info("ws: client requested set_mode=%r (current mode=%s)", requested, backend.mode.value)
        await backend.set_mode(requested)
        await _broadcast_state()
    elif mtype == protocol.GET_STATE:
        await _broadcast_state()
    elif mtype == protocol.RUN_COMMAND:
        await backend.run_command(data.get("command", ""))
    elif mtype == protocol.START_LISTENING:
        await backend.start_listening()
    elif mtype == protocol.STOP:
        await backend.stop()


# Registered last: a catch-all mount must not shadow the routes above.
app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
