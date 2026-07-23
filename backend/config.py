"""All constants live here. Nothing hardcoded elsewhere."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

HOST = "127.0.0.1"
PORT = 8765

FRONTEND_DIR = BASE_DIR / "frontend"
PERSONALITY_FILE = BASE_DIR / "data" / "personality.json"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Text mode uses the same model family as voice mode (Gemini Live) so Jarvis
# doesn't have two different personalities depending on the mode.
GEMINI_TEXT_MODEL = "gemini-3.6-flash"
GEMINI_MAX_OUTPUT_TOKENS = 1000
# temperature/top_p/top_k and thinking_level are intentionally left unset —
# Google recommends defaults for Gemini 3.x, and the minimal default thinking
# level is right for conversational latency here.

# How many prior history entries (user + jarvis turns combined) are sent to the
# router as conversation context. Keeps the prompt bounded as history grows.
ROUTER_HISTORY_LIMIT = 20

# Voice mode: persistent Gemini Live session (backend/live_session.py).
LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.1-flash-live-preview")
VOICE_NAME = os.getenv("VOICE_NAME", "Charon")

# Voice mode: microphone (backend/audio/mic.py). Substring match against
# sounddevice's device names; falls back to the system default input device.
MIC_NAME_HINT = os.getenv("MIC_NAME_HINT", "FIFINE")

# Audio stream shape, shared by mic.py, player.py and live_session.py. A
# consumer that needs different framing (e.g. OpenWakeWord's 1280-sample
# frames in step 5c) re-buffers on its own side — these stay fixed here.
SAMPLE_RATE_IN = 16000
SAMPLE_RATE_OUT = 24000
CHUNK_SIZE = 1024

# Off by default: at ~16 chunks/sec, per-chunk send logging floods the log
# bus (and, through it, the WebSocket transcript channel) badly enough to
# visibly delay transcript delivery — this is not cosmetic. Flip on only
# when actually debugging the audio send path.
DEBUG_AUDIO_CHUNKS = os.getenv("DEBUG_AUDIO_CHUNKS", "false").lower() in ("1", "true", "yes")
