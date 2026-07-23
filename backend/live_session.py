"""Persistent Gemini Live session.

One WebSocket lives for the whole voice-mode period. `turn_complete` finalises
the current turn and appends it to history but does NOT reconnect — the
previous version reconnected every turn, which caused mic churn and lost
speech onsets; that bug is specifically what this design avoids. Reconnect
only happens on a dropped/expiring connection (this file, step 5b) or when
voice mode is switched off (facade.set_mode).

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

Step 5b (this revision) adds session continuity on top of that, none of it
present in the reference (the old project never held a session long enough to
need it):
  - context window compression, so an audio-only session isn't capped at
    ~15 minutes of accumulated tokens
  - session resumption with a stored handle, so the ~10-minute connection
    lifetime doesn't kill the conversation. Plain (non-transparent) mode:
    the Developer API doesn't support `transparent=True` (Vertex/Enterprise
    only), so there is no last_consumed_client_message_index telling us what
    the server actually consumed before a drop. Chunks already handed to the
    wire when a connection dies are therefore treated as lost, not replayed
    — guessing and resending risks the server having already gotten them,
    and duplicated PCM garbles the model's understanding just as badly as
    reordered PCM does. A brief silence across the reconnect is the safe
    outcome, not a guess.
  - proactive reconnect on GoAway, plus backoff-retried reconnect on an
    unexpected drop, with the mic/player left running throughout
  - one ordered send queue that every mic chunk passes through, whether the
    connection is up or not, so recovering from a drop can never reorder or
    duplicate audio the way a separate "replay what we buffered" pass could —
    see _drain_send_queue for why that matters (garbled/reordered PCM makes
    the model hallucinate a plausible-sounding sentence from scrambled audio,
    which is exactly the failure mode 5a already fought once)
"""

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Deque, List, Optional, Tuple

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
FatalErrorCallback = Callable[[str], Awaitable[None]]

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
        on_fatal_error: FatalErrorCallback,
    ) -> None:
        self._set_state = set_state
        self._broadcast_transcript = broadcast_transcript
        self._append_history = append_history
        self._get_history = get_history
        self._on_fatal_error = on_fatal_error

        self._client: Optional[genai.Client] = None
        self._session_cm = None
        self._session = None
        self._mic_queue: "Optional[asyncio.Queue[bytes]]" = None
        self._send_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing = False

        # -- session continuity state (step 5b) --
        # Whether self._session is currently a live, usable connection. False
        # for the whole window between detecting a drop and a successful
        # reconnect; _drain_send_queue stops sending (without discarding
        # anything) while it's False.
        self._connected_ok = False
        self._resumption_handle: Optional[str] = None
        # The one queue every mic chunk passes through before being sent —
        # not sent directly, ever, connected or not. That's what keeps a
        # reconnect from being able to reorder or duplicate audio: there is
        # only one path out (_drain_send_queue) and only one order (FIFO).
        # Continuously trimmed (see _enqueue_send) so a long reconnect can't
        # grow it without bound.
        self._send_queue: Deque[Tuple[bytes, float]] = deque()

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

        self._closing = False
        self._reset_turn_state()
        self._resumption_handle = None
        self._send_queue = deque()
        self._reconnect_task = None

        await self._connect(handle=None)
        self._connected_ok = True

        self._send_task = asyncio.create_task(self._send_loop())
        self._receive_task = asyncio.create_task(self._receive_loop())

        await self._set_state(State.LISTENING)

    async def _connect(self, handle: Optional[str]) -> None:
        """Open one Live connection. Raises on failure — callers (start() and
        the reconnect loop) decide what to do about that."""
        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=build_system_prompt(self._get_history()),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.VOICE_NAME))
            ),
            # no tools: deliberate for this step
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
                trigger_tokens=config.LIVE_COMPRESSION_TRIGGER_TOKENS,
            ),
            session_resumption=types.SessionResumptionConfig(handle=handle),
        )

        session_cm = self._client.aio.live.connect(model=config.LIVE_MODEL, config=live_config)
        session = await session_cm.__aenter__()
        self._session_cm = session_cm
        self._session = session
        logger.info(
            "live: session connected (model=%s, voice=%s, resuming=%s)",
            config.LIVE_MODEL,
            config.VOICE_NAME,
            handle is not None,
        )

    async def stop(self) -> None:
        logger.debug("live: stop() called")
        self._closing = True

        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("live: error while cancelling in-flight reconnect")
            self._reconnect_task = None

        if self._session is not None:
            # Google's guidance: signal end-of-stream so the server flushes
            # whatever audio it has cached instead of waiting on more that
            # will never come — applies to every deliberate stop, not just this
            # one, but this is currently the only place voice mode stops.
            try:
                await self._session.send_realtime_input(audio_stream_end=True)
            except Exception:
                logger.debug("live: failed to send audio_stream_end (ignored, shutting down anyway)", exc_info=True)

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
        self._connected_ok = False
        logger.info("live: session stopped, mic released")

    async def stop_playback(self) -> None:
        """Manual barge-in (the `stop` protocol message): silence immediately
        and drop the rest of the turn already in flight from the server, so
        playback doesn't fall silent and then resume from a stale buffer."""
        self._suppress_turn_audio = True
        player.flush()

    # -- send loop -----------------------------------------------------

    def _trim_send_queue(self, now: float) -> None:
        dropped = 0
        while self._send_queue and (now - self._send_queue[0][1]) > config.LIVE_RECONNECT_AUDIO_MAX_GAP_S:
            self._send_queue.popleft()
            dropped += 1
        if dropped:
            logger.debug("live: dropped %d aged-out queued audio chunks", dropped)

    def _enqueue_send(self, chunk: bytes) -> None:
        now = time.monotonic()
        self._send_queue.append((chunk, now))
        self._trim_send_queue(now)

    async def _send_loop(self) -> None:
        assert self._mic_queue is not None
        try:
            while True:
                chunk = await self._mic_queue.get()
                self._enqueue_send(chunk)
                await self._drain_send_queue()
        except asyncio.CancelledError:
            pass

    async def _drain_send_queue(self) -> None:
        """The only path audio ever goes out through — normal operation and
        catching up after a reconnect alike. Sends strictly one chunk at a
        time, in queue order, and stops the moment the connection isn't
        usable rather than sending anything out of turn. There is no second
        code path that sends audio directly, so audio can't be reordered
        against what's still queued.

        No transparent resumption on the Developer API means no
        last_consumed_client_message_index — there is no way to know what
        the server actually received before a drop. So a chunk in flight
        when the connection dies is simply discarded, never resent: resending
        on a guess risks the server having already gotten it, and duplicated
        PCM confuses the model as badly as reordered PCM does. A brief gap
        is the accepted, safe outcome — see _reconnect for where it's logged.
        """
        while self._send_queue:
            if not self._connected_ok:
                return

            now = time.monotonic()
            self._trim_send_queue(now)
            if not self._send_queue:
                return

            chunk, _ts = self._send_queue[0]
            session = self._session  # snapshot: detect if a reconnect swaps this out mid-await
            if config.DEBUG_AUDIO_CHUNKS:
                logger.debug("live: sending audio chunk (%d bytes)", len(chunk))
            try:
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={config.SAMPLE_RATE_IN}")
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._closing:
                    return
                if session is not self._session:
                    # This send was against a connection a reconnect already
                    # superseded while it was in flight — it failed, so it
                    # never reached a live server; safe to retry as-is on the
                    # current connection, no duplication risk.
                    logger.debug("live: send failed on a superseded connection, retrying on the current one")
                    continue
                # A genuine live drop. Whether this chunk (or ones sent just
                # before it) actually reached the server first is unknowable
                # without transparent resumption — see the docstring above.
                self._send_queue.popleft()
                logger.warning(
                    "live: send failed, connection dropped — discarding in-flight audio, reconnecting", exc_info=True
                )
                self._trigger_reconnect("dropped")
                return
            else:
                if session is not self._session:
                    # Reported success, but against a connection already
                    # superseded — genuinely ambiguous whether the server
                    # got it. Don't resend: that risks duplicating audio the
                    # server may already have. Accept the possible gap
                    # instead of guessing.
                    logger.debug(
                        "live: a send against a superseded connection reported success; "
                        "not resending (ambiguous delivery)"
                    )
                self._send_queue.popleft()

    # -- receive loop --------------------------------------------------

    async def _receive_loop(self) -> None:
        # session.receive() (the SDK call) is scoped to a single turn: it
        # yields events and returns as soon as one carrying turn_complete is
        # seen. It must be re-entered for every turn, on this same
        # connection, or the app silently stops reading server responses
        # after turn 1 while the connection itself stays open. The outer loop
        # here is that re-entry — not a reconnect.
        try:
            while not self._closing:
                if not self._connected_ok or self._session is None:
                    # A reconnect (proactive GoAway or a detected drop) is
                    # already in flight — wait for it rather than touching a
                    # session that doesn't exist yet.
                    if self._reconnect_task is not None:
                        await self._reconnect_task
                    if self._closing:
                        break
                    continue

                session = self._session  # snapshot: detect if a reconnect swaps this out mid-iteration
                try:
                    async for response in session.receive():
                        await self._handle_event(response)
                    logger.debug("live: turn's receive() exhausted, requesting next turn")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._closing:
                        break
                    if session is not self._session:
                        # This connection was already superseded by a
                        # reconnect (e.g. GoAway closed it out from under us)
                        # — not a new drop, just loop back onto the current
                        # session.
                        logger.debug("live: receive loop error on a superseded connection, ignoring")
                        continue
                    # A real drop (server closed the connection under us) —
                    # distinct from cancellation, which is deliberate shutdown
                    # (self._closing, checked above) and must not be treated
                    # as a reconnect-worthy failure.
                    logger.warning("live: receive loop dropped (connection error), reconnecting", exc_info=True)
                    self._trigger_reconnect("dropped")
                    if self._reconnect_task is not None:
                        await self._reconnect_task
                    if self._closing:
                        break
        except asyncio.CancelledError:
            pass

    async def _handle_event(self, response: types.LiveServerMessage) -> None:
        if response.tool_call is not None:
            logger.debug("live: ignoring unexpected tool_call (no tools registered)")

        if response.go_away is not None:
            logger.info("live: GoAway received (time_left=%s), reconnecting proactively", response.go_away.time_left)
            self._trigger_reconnect("goaway")

        if response.session_resumption_update is not None:
            upd = response.session_resumption_update
            if upd.new_handle:
                self._resumption_handle = upd.new_handle
            # No last_consumed_client_message_index without transparent mode
            # (Developer API doesn't support it) — nothing to prune here.
            logger.debug("live: session_resumption_update (resumable=%s)", upd.resumable)

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

    # -- reconnect (step 5b) --------------------------------------------

    def _trigger_reconnect(self, reason: str) -> None:
        """Idempotent: safe to call from both loops when the same drop trips
        both of them — only the first call actually starts a reconnect."""
        if self._closing:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect(reason))

    async def _reconnect(self, reason: str) -> None:
        self._connected_ok = False
        gap_started = time.monotonic()
        await self._set_state(State.RECONNECTING)
        logger.info("live: reconnecting (%s)", reason)

        # Whatever's still queued simply stays in self._send_queue — nobody
        # sends it, and _enqueue_send keeps trimming it as new mic chunks
        # arrive, so it can't grow without bound no matter how long this
        # takes.

        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("live: error closing old session during reconnect (ignored)", exc_info=True)
            self._session_cm = None
            self._session = None

        for attempt in range(1, config.LIVE_RECONNECT_MAX_RETRIES + 1):
            if self._closing:
                return
            try:
                await self._connect(self._resumption_handle)
                gap = time.monotonic() - gap_started
                logger.info(
                    "live: reconnected after %.1fs gap (attempt %d/%d, reason=%s)",
                    gap,
                    attempt,
                    config.LIVE_RECONNECT_MAX_RETRIES,
                    reason,
                )
                break
            except Exception as e:
                logger.warning(
                    "live: reconnect attempt %d/%d failed: %s", attempt, config.LIVE_RECONNECT_MAX_RETRIES, e
                )
                # Docs disagree on how long a resumption handle stays valid.
                # Rather than depend on a number, treat any failed reconnect
                # as reason enough to drop it and try fresh next attempt.
                self._resumption_handle = None
                delay = min(
                    config.LIVE_RECONNECT_BACKOFF_BASE_S * (2 ** (attempt - 1)), config.LIVE_RECONNECT_BACKOFF_MAX_S
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
        else:
            if not self._closing:
                logger.error(
                    "live: giving up after %d reconnect attempts (%s)", config.LIVE_RECONNECT_MAX_RETRIES, reason
                )
                await self._give_up()
            return

        if self._closing:
            return

        self._connected_ok = True
        await self._set_state(State.LISTENING)
        # Deliberately nothing else here: _send_loop's own next mic chunk
        # calls _drain_send_queue(), which is the one place that sends, and
        # it will work through whatever's queued (oldest first) before
        # sending anything new — see _drain_send_queue.

    async def _give_up(self) -> None:
        """Reconnect attempts exhausted: this is a fatal end to the voice
        session, not just this connection. Tear down the same resources
        stop() would, then hand off to the facade via on_fatal_error so mode
        state doesn't end up claiming voice mode is still active."""
        self._closing = True

        current = asyncio.current_task()
        for task in (self._send_task, self._receive_task):
            if task is not None and task is not current:
                task.cancel()

        if self._mic_queue is not None:
            mic.unsubscribe(self._mic_queue)
            self._mic_queue = None
        mic.stop()
        await player.stop()
        self._connected_ok = False

        await self._on_fatal_error("Voice connection lost and could not be restored.")
        await self._set_state(State.IDLE)
