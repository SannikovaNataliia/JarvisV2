"""Persistent Gemini Live session.

One WebSocket lives for the whole voice-mode period. `turn_complete` finalises
the current turn and appends it to history but does NOT reconnect — the
previous version reconnected every turn, which caused mic churn and lost
speech onsets; that bug is specifically what this design avoids. Reconnect
only happens on error or when voice mode is switched off (facade.set_mode).

The SDK's `session.receive()` is itself scoped to a single turn (it returns
once it yields a turn_complete event), so `_receive_loop` re-enters it in a
`while not self._closing` loop — one connection, requesting the next turn's
messages each time — rather than treating "receive() returned" as a reason to
tear the session down.

Read _reference/core/gemini_live.py and _reference/core/Jarvis.py to
understand the session shape (LiveConnectConfig, the receive-loop event
fields, sd.InputStream/RawOutputStream usage) before writing this file, then
reimplemented cleanly:
  - dropped the reference's TurnComplete-exception-triggers-reconnect pattern
  - dropped its PyAudio (wake word) + sounddevice (Live) dual mic ownership
  - replaced `session.send_realtime_input(media={...})` (old SDK shape) with
    `send_realtime_input(audio=types.Blob(...))`, matching the SDK installed
    here
  - added no-tools config, shared history/personality seeding, explicit
    interim- vs. finalised-input-transcription handling, and barge-in
    suppression, none of which the reference had
Nothing was imported, copied, or executed from _reference/.
"""

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional

from google import genai
from google.genai import types

from backend import config
from backend.audio import mic, player
from backend.enums import State
from backend.personality import build_system_prompt

logger = logging.getLogger(__name__)

SetStateCallback = Callable[[State], Awaitable[None]]
BroadcastTranscriptCallback = Callable[[str, str, bool], Awaitable[None]]
AppendHistoryCallback = Callable[[str, str], None]
GetHistoryCallback = Callable[[], List[dict]]

# Model availability is a property of the API key + model name, not of any one
# voice-mode entry, so it's checked once for the process and cached — see
# check_model_availability(), called from server.py's startup event. Without
# this, every voice-mode toggle re-did the same network round trip.
_model_checked = False
_model_available = False
_model_check_error: Optional[Exception] = None


async def check_model_availability() -> None:
    global _model_checked, _model_available, _model_check_error
    if _model_checked:
        return
    client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={"api_version": "v1beta"})
    try:
        await client.aio.models.get(model=config.LIVE_MODEL)
        _model_available = True
        logger.info("live: model %r verified available", config.LIVE_MODEL)
    except Exception as e:
        _model_available = False
        _model_check_error = e
        logger.error("live: model %r is not available for this API key", config.LIVE_MODEL, exc_info=True)
    finally:
        _model_checked = True


class LiveSession:
    """Owned by the facade for the duration of one voice-mode period.

    Talks back to the facade only through the callbacks passed in, the same
    pattern the facade already uses to talk to the server (on_state /
    on_transcript) — this class has no reference to the facade itself.
    """

    def __init__(
        self,
        set_state: SetStateCallback,
        broadcast_transcript: BroadcastTranscriptCallback,
        append_history: AppendHistoryCallback,
        get_history: GetHistoryCallback,
    ) -> None:
        self._set_state = set_state
        self._broadcast_transcript = broadcast_transcript
        self._append_history = append_history
        self._get_history = get_history

        self._client: Optional[genai.Client] = None
        self._session_cm = None
        self._session = None
        self._mic_queue: "Optional[asyncio.Queue[bytes]]" = None
        self._send_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._closing = False

        self._reset_turn_state()

    def _reset_turn_state(self) -> None:
        self._user_text = ""
        self._jarvis_text = ""
        self._user_turn_ended = False
        self._speaking = False
        self._suppress_turn_audio = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        logger.debug("live: start() called")

        if not _model_checked:
            # Normally already done once at server startup — this only runs
            # here if start() is called before that (e.g. a standalone script).
            await check_model_availability()
        if not _model_available:
            raise RuntimeError(f"live model {config.LIVE_MODEL!r} is not available: {_model_check_error}")

        self._client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={"api_version": "v1beta"})

        await mic.start()
        self._mic_queue = mic.subscribe()
        await player.start()

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=build_system_prompt(self._get_history()),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.VOICE_NAME))
            ),
            # no tools: deliberate for this step
        )

        self._session_cm = self._client.aio.live.connect(model=config.LIVE_MODEL, config=live_config)
        self._session = await self._session_cm.__aenter__()
        logger.info("live: session connected (model=%s, voice=%s)", config.LIVE_MODEL, config.VOICE_NAME)

        self._closing = False
        self._reset_turn_state()
        self._send_task = asyncio.create_task(self._send_loop())
        self._receive_task = asyncio.create_task(self._receive_loop())

        await self._set_state(State.LISTENING)

    async def stop(self) -> None:
        logger.debug("live: stop() called")
        self._closing = True

        for task in (self._send_task, self._receive_task):
            if task is not None:
                task.cancel()
        for task in (self._send_task, self._receive_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("live: error while stopping a session task")
        self._send_task = None
        self._receive_task = None

        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                logger.exception("live: error closing session")
            self._session_cm = None
            self._session = None

        if self._mic_queue is not None:
            mic.unsubscribe(self._mic_queue)
            self._mic_queue = None
        mic.stop()
        await player.stop()
        logger.info("live: session stopped, mic released")

    async def stop_playback(self) -> None:
        """Manual barge-in (the `stop` protocol message): silence immediately
        and drop the rest of the turn already in flight from the server, so
        playback doesn't fall silent and then resume from a stale buffer."""
        self._suppress_turn_audio = True
        player.flush()

    # -- send loop -----------------------------------------------------

    async def _send_loop(self) -> None:
        assert self._mic_queue is not None
        try:
            while True:
                chunk = await self._mic_queue.get()
                if config.DEBUG_AUDIO_CHUNKS:
                    logger.debug("live: sending audio chunk (%d bytes)", len(chunk))
                await self._session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={config.SAMPLE_RATE_IN}")
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            if not self._closing:
                logger.exception("live: send loop failed")

    # -- receive loop --------------------------------------------------

    async def _receive_loop(self) -> None:
        # session.receive() (the SDK call) is scoped to a single turn: it
        # yields events and returns as soon as one carrying turn_complete is
        # seen. It must be re-entered for every turn, on this same
        # connection, or the app silently stops reading server responses
        # after turn 1 while the connection itself stays open. The outer loop
        # here is that re-entry — not a reconnect.
        #
        # self._closing (set by stop(), before it cancels this task) is what
        # tells this loop a shutdown is deliberate: the CancelledError that
        # follows is expected and swallowed. Without that flag, a real drop
        # (server closes the connection under us) is indistinguishable from
        # cancellation and would be silently swallowed too.
        try:
            while not self._closing:
                async for response in self._session.receive():
                    await self._handle_event(response)
                logger.debug("live: turn's receive() exhausted, requesting next turn")
        except asyncio.CancelledError:
            pass
        except Exception:
            if not self._closing:
                logger.exception("live: receive loop failed, dropping connection")
                await self._set_state(State.IDLE)

    async def _handle_event(self, response: types.LiveServerMessage) -> None:
        if response.tool_call is not None:
            logger.debug("live: ignoring unexpected tool_call (no tools registered)")

        sc = response.server_content
        if sc is None:
            return

        # User is still speaking: low-latency deltas, stream them but stay
        # LISTENING — this is not "the turn ended", just live feedback.
        if sc.interim_input_transcription and sc.interim_input_transcription.text:
            text = sc.interim_input_transcription.text
            self._user_text += text
            await self._broadcast_transcript("user", text, False)

        # Finalised input transcription is the server's signal that the
        # user's turn has ended and generation is starting -> THINKING.
        if sc.input_transcription and sc.input_transcription.text:
            text = sc.input_transcription.text
            self._user_text += text
            await self._broadcast_transcript("user", text, False)
            if not self._user_turn_ended:
                self._user_turn_ended = True
                await self._set_state(State.THINKING)

        if sc.model_turn and sc.model_turn.parts:
            for part in sc.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    if not self._suppress_turn_audio:
                        player.play(part.inline_data.data)
                    # Fallback in case input_transcription never arrived for
                    # this turn: audio arriving is unambiguous proof the
                    # user's turn ended, so the state machine can't get stuck.
                    self._user_turn_ended = True
                    if not self._speaking:
                        self._speaking = True
                        await self._set_state(State.SPEAKING)
                elif part.thought:
                    logger.debug("live: model_turn thought part (ignored)")
                elif part.text:
                    logger.debug("live: unexpected text part in AUDIO-modality model_turn: %r", part.text)
                else:
                    logger.debug("live: unhandled model_turn part: %r", part)

        if sc.output_transcription and sc.output_transcription.text:
            text = sc.output_transcription.text
            self._jarvis_text += text
            await self._broadcast_transcript("jarvis", text, False)
            self._user_turn_ended = True
            if not self._speaking:
                self._speaking = True
                await self._set_state(State.SPEAKING)

        if sc.interrupted:
            logger.debug("live: interrupted by user, flushing playback")
            player.flush()
            await self._finish_turn()

        if sc.turn_complete:
            await self._finish_turn()

    async def _finish_turn(self) -> None:
        if self._user_text:
            await self._broadcast_transcript("user", "", True)
            self._append_history("user", self._user_text)
        if self._jarvis_text:
            await self._broadcast_transcript("jarvis", "", True)
            self._append_history("jarvis", self._jarvis_text)
        self._reset_turn_state()
        await self._set_state(State.LISTENING)
