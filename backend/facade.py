"""JarvisBackend — the only thing the server talks to. Knows nothing about WebSockets."""

import logging
from typing import Awaitable, Callable, List, Optional

from backend import config
from backend.enums import Mode, State
from backend.live_session import LiveSession
from backend.router import answer

logger = logging.getLogger(__name__)

StateCallback = Callable[[State], Awaitable[None]]
TranscriptCallback = Callable[[str, str, bool], Awaitable[None]]
ErrorCallback = Callable[[str, bool], Awaitable[None]]


class JarvisBackend:
    def __init__(self) -> None:
        self._state = State.IDLE
        self._mode = Mode.TEXT
        self.history: List[dict] = []
        self._on_state: List[StateCallback] = []
        self._on_transcript: List[TranscriptCallback] = []
        self._on_error: List[ErrorCallback] = []
        self._live_session: Optional[LiveSession] = None

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

    def on_error(self, callback: ErrorCallback) -> None:
        self._on_error.append(callback)

    def get_state(self) -> dict:
        return {"state": self._state.value, "mode": self._mode.value, "history": list(self.history)}

    async def _set_state(self, new_state: State) -> None:
        self._state = new_state
        for cb in self._on_state:
            await cb(new_state)

    async def _broadcast_transcript(self, role: str, text: str, final: bool) -> None:
        for cb in self._on_transcript:
            await cb(role, text, final)

    async def _broadcast_error(self, message: str, fatal: bool) -> None:
        for cb in self._on_error:
            await cb(message, fatal)

    async def _handle_live_fatal_error(self, message: str) -> None:
        """LiveSession calls this when reconnect attempts are exhausted — a
        fatal end to the voice session, not just one connection. Reset mode
        state here (before broadcasting) so the state message that follows
        reports mode=text, not a voice mode that's actually dead."""
        logger.error("facade: voice session failed permanently: %s", message)
        self._live_session = None
        self._mode = Mode.TEXT
        await self._broadcast_error(message, True)

    def _append_history(self, role: str, text: str) -> None:
        if text:
            self.history.append({"role": role, "text": text})

    async def _emit_transcript(self, role: str, text: str, final: bool = True) -> None:
        """Text mode: one full message, both the UI event and the history entry
        together. Voice mode streams many partial chunks per turn instead and
        only appends to history once, on turn completion — see live_session.py."""
        self._append_history(role, text)
        await self._broadcast_transcript(role, text, final)

    async def set_mode(self, mode: str) -> None:
        if mode == self._mode.value:
            logger.debug("facade.set_mode: already in mode %r, ignoring", mode)
            return

        logger.info("facade.set_mode: %s -> %s", self._mode.value, mode)

        if mode == Mode.VOICE.value:
            session = LiveSession(
                set_state=self._set_state,
                broadcast_transcript=self._broadcast_transcript,
                append_history=self._append_history,
                get_history=lambda: self.history,
                on_fatal_error=self._handle_live_fatal_error,
            )
            try:
                await session.start()
            except Exception:
                logger.exception("facade.set_mode: failed to start voice mode")
                await self._set_state(State.IDLE)
                return
            self._live_session = session
            self._mode = Mode.VOICE
        elif mode == Mode.TEXT.value:
            if self._live_session is not None:
                logger.debug("facade.set_mode: stopping live session for text mode")
                await self._live_session.stop()
                self._live_session = None
            self._mode = Mode.TEXT
            await self._set_state(State.IDLE)
        else:
            logger.warning("set_mode: unknown mode %r", mode)

    async def send_text(self, text: str) -> None:
        await self._set_state(State.THINKING)
        recent = self.history[-config.ROUTER_HISTORY_LIMIT :]
        await self._emit_transcript("user", text)
        reply = await answer(text, recent)
        await self._emit_transcript("jarvis", reply)
        await self._set_state(State.IDLE)

    async def run_command(self, command: str) -> None:
        logger.warning("run_command not implemented: %s", command)

    async def start_listening(self) -> None:
        # No wake word yet (step 5c): voice mode already listens continuously
        # once connected, so there is nothing separate to trigger here.
        logger.debug("start_listening: no-op, voice mode already listens continuously")

    async def stop(self) -> None:
        if self._live_session is not None:
            await self._live_session.stop_playback()
        else:
            logger.debug("stop: no active voice session")
