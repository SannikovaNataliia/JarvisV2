"""Shared system-prompt builder.

Used by both the text router and the Live session so Jarvis has one
personality regardless of mode. The two paths seed conversation context
differently: the text router sends prior turns as `contents` alongside this
prompt, while a Live session has no such mechanism, so it passes `history`
here to have it embedded directly in the system instruction text.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from backend import config

logger = logging.getLogger(__name__)


def build_system_prompt(history: Optional[List[dict]] = None) -> str:
    try:
        p = json.loads(Path(config.PERSONALITY_FILE).read_text(encoding="utf-8"))
    except Exception:
        logger.exception("failed to load personality file, using fallback prompt")
        return "You are Jarvis, a personal AI assistant."

    lines = [
        "You are Jarvis, a personal AI assistant.",
        "",
        f"Core traits: {p.get('core_traits', '')}",
        "",
        "Speech rules:",
        *(f"- {rule}" for rule in p.get("speech_rules", [])),
    ]
    if p.get("conversation_style"):
        lines += ["", f"Conversation style: {p['conversation_style']}"]
    if p.get("behavior_rules"):
        lines += ["", "Behavior rules:", *(f"- {rule}" for rule in p["behavior_rules"])]

    now = datetime.now().strftime("%A, %B %d %Y, %H:%M")
    lines += ["", f"Current date and time: {now}"]

    if history:
        lines += ["", "Previous conversation:"]
        lines += [f"{'Jarvis' if turn['role'] == 'jarvis' else 'User'}: {turn['text']}" for turn in history]

    return "\n".join(lines)
