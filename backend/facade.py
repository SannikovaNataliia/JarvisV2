"""JarvisBackend — the only thing the server talks to. Knows nothing about WebSockets."""

import logging
from enum import Enum
from typing import Awaitable, Callable, List

from backend import config
from backend.router import answer

logger = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    WAKING = "waking"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Mode(str, Enum):
    VOICE = "voice"
    TEXT = "text"


StateCallback = Callable[[State], Awaitable[None]]
TranscriptCallback = Callable[[str, str, bool], Awaitable[None]]


class JarvisBackend:
    def __init__(self) -> None:
        self._state = State.IDLE
        self._mode = Mode.TEXT
        self.history: List[dict] = []
        self._on_state: List[StateCallback] = []
        self._on_transcript: List[TranscriptCallback] = []

    @property
    def state(self) -> State:
        return self._state

    @property
    def mode(self) -> Mode:
        return self._mode

    def on_state(self, callback: StateCallback) -> None:
        self._on_state.append(callback)

    def on_transcript(self, callback: TranscriptCallback) -> None:
        self._on_transcript.append(callback)

    def get_state(self) -> dict:
        return {"state": self._state.value, "mode": self._mode.value, "history": list(self.history)}

    async def _set_state(self, new_state: State) -> None:
        self._state = new_state
        for cb in self._on_state:
            await cb(new_state)

    async def _emit_transcript(self, role: str, text: str, final: bool = True) -> None:
        self.history.append({"role": role, "text": text})
        for cb in self._on_transcript:
            await cb(role, text, final)

    def set_mode(self, mode: str) -> None:
        if mode == Mode.VOICE.value:
            logger.warning("voice mode not implemented yet")
            return
        self._mode = Mode.TEXT

    async def send_text(self, text: str) -> None:
        await self._set_state(State.THINKING)
        recent = self.history[-config.ROUTER_HISTORY_LIMIT:]
        await self._emit_transcript("user", text)
        reply = await answer(text, recent)
        await self._emit_transcript("jarvis", reply)
        await self._set_state(State.IDLE)

    async def run_command(self, command: str) -> None:
        logger.warning("run_command not implemented: %s", command)

    async def start_listening(self) -> None:
        logger.warning("start_listening not implemented")

    async def stop(self) -> None:
        logger.warning("stop not implemented")
