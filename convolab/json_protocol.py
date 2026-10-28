"""Strict complete JSON-object decoding shared by state and survey validation."""
from __future__ import annotations

import json


class JsonProtocolError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JsonProtocolError("Duplicate JSON keys are not allowed.")
        result[key] = value
    return result


def _constant(value):
    raise JsonProtocolError("Non-standard JSON constants are not allowed.")


def _decoder():
    # Each validation gets its own decoder/memo; parallel workers share no
    # mutable parsing workspace.
    return json.JSONDecoder(object_pairs_hook=_object, parse_constant=_constant)


def strict_json_loads(text: str):
    """Whole text only, including rejection of duplicate keys at any depth."""
    return _decoder().decode(text)


def decode_json_object(text: str) -> dict:
    """§6.3/6.4: whole text first, then first complete container, never merge."""
    text = text.strip()
    decoder = _decoder()
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = decoder.decode(text)
    except json.JSONDecodeError:
        # A complete container may be surrounded by prose. An array enclosing
        # an object is itself the first container and must be rejected.
        for position, char in enumerate(text):
            if char not in "{[":
                continue
            try:
                value, _ = decoder.raw_decode(text, position)
            except json.JSONDecodeError:
                continue
            break
        else:
            raise JsonProtocolError("The response contains no complete JSON object.") from None
    # Protocol errors from duplicate keys/constants propagate immediately,
    # rather than searching for a later acceptable object.
    if not isinstance(value, dict):
        raise JsonProtocolError("The response must be one JSON object, not an array or scalar.")
    return value
