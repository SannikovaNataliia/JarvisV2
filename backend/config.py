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
