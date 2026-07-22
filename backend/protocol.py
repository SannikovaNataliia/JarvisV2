"""Message envelope and type constants — the single source of truth for the wire format.

{ "type": "...", "id": "uuid4", "ts": 1721600000.123, "data": {} }

Only send_text / set_mode / get_state (inbound) and ready / state / transcript /
log / error (outbound) are implemented in this build. The rest are declared so
the constant exists everywhere it will eventually be used, but are not yet
handled by the server or facade.
"""

import json
import time
import uuid
from typing import Optional

# Frontend -> backend
SEND_TEXT = "send_text"
RUN_COMMAND = "run_command"
SET_MODE = "set_mode"
START_LISTENING = "start_listening"
STOP = "stop"
GET_STATE = "get_state"

# Backend -> frontend
READY = "ready"
STATE = "state"
TRANSCRIPT = "transcript"
TOOL_CALL = "tool_call"
LOG = "log"
ERROR = "error"

INBOUND_TYPES = {SEND_TEXT, RUN_COMMAND, SET_MODE, START_LISTENING, STOP, GET_STATE}
OUTBOUND_TYPES = {READY, STATE, TRANSCRIPT, TOOL_CALL, LOG, ERROR}


class ProtocolError(ValueError):
    pass


def build(msg_type: str, data: Optional[dict] = None, msg_id: Optional[str] = None) -> dict:
    return {
        "type": msg_type,
        "id": msg_id or str(uuid.uuid4()),
        "ts": time.time(),
        "data": data or {},
    }


def dumps(msg_type: str, data: Optional[dict] = None, msg_id: Optional[str] = None) -> str:
    return json.dumps(build(msg_type, data, msg_id))


def parse(raw: str) -> dict:
    """Parse and validate an inbound message. Raises ProtocolError on anything malformed —
    callers must catch this and respond with an `error` message, never let it propagate."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"malformed JSON: {e}") from e

    if not isinstance(msg, dict):
        raise ProtocolError("message must be a JSON object")
    if "type" not in msg or not isinstance(msg["type"], str):
        raise ProtocolError("message missing string 'type'")
    if msg["type"] not in INBOUND_TYPES:
        raise ProtocolError(f"unknown inbound type: {msg['type']}")

    msg.setdefault("id", str(uuid.uuid4()))
    msg.setdefault("ts", time.time())
    msg.setdefault("data", {})
    if not isinstance(msg["data"], dict):
        raise ProtocolError("'data' must be an object")

    return msg
