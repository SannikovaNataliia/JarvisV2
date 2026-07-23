"""State/Mode enums, kept out of facade.py so live_session.py can import them
without a facade <-> live_session circular import."""

from enum import Enum


class State(str, Enum):
    IDLE = "idle"
    WAKING = "waking"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Mode(str, Enum):
    VOICE = "voice"
    TEXT = "text"
