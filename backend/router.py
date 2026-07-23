"""Minimal text-mode router.

Uses Gemini Flash — the same model family as voice mode's Gemini Live — so
Jarvis has one consistent personality regardless of which mode is active.

Originally reimplemented from _reference/core/router.py's groq_answer(); kept
only the single-model conversational path (personality system prompt +
history + model call). Everything else in that file (Claude, web search,
command/app routing, multi-action dispatch) is out of scope for this build
and intentionally dropped.
"""

import logging
from typing import List, Optional

from google import genai
from google.genai import types

from backend import config
from backend.personality import build_system_prompt

logger = logging.getLogger(__name__)

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={"api_version": "v1beta"})
    return _client


async def answer(text: str, history: List[dict]) -> str:
    """history: prior turns as {"role": "user"|"jarvis", "text": str}, oldest first.
    `text` is the new user message and must NOT already be included in `history`."""
    try:
        client = _get_client()
        contents = [
            types.Content(
                role="model" if turn["role"] == "jarvis" else "user",
                parts=[types.Part(text=turn["text"])],
            )
            for turn in history
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))

        # Gemini 3.x rejects a contents array whose last turn has role "model".
        # The unconditional append above already guarantees this, but trim
        # defensively so a future change to this function can't silently break it.
        while contents and contents[-1].role == "model":
            contents.pop()

        config_ = types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
        )

        response = await client.aio.models.generate_content(
            model=config.GEMINI_TEXT_MODEL,
            contents=contents,
            config=config_,
        )
        return response.text.strip()
    except Exception:
        logger.error("router.answer failed", exc_info=True)
        return "Something went wrong on my end."
