"""Helpers that turn raw model text into clean text or validated JSON objects.

Used by the LLM gateway's fallback path (a plain completion parsed as JSON) and for streamed text:
reasoning models may wrap their thoughts in ``<think>...</think>`` and small models like to wrap
JSON in markdown fences, so both are stripped before parsing or displaying anything.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[A-Za-z0-9_-]*\s*\n?(.*?)```", re.DOTALL)
_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"


def strip_think(text: str) -> str:
    """Remove ``<think>...</think>`` blocks, a dangling unterminated ``<think>`` tail and an orphan ``</think>``."""
    cleaned = _THINK_BLOCK.sub("", text)
    lowered = cleaned.lower()
    if _CLOSE_TAG in lowered:  # reasoning without an opening tag: keep only what follows it
        cleaned = cleaned[lowered.rfind(_CLOSE_TAG) + len(_CLOSE_TAG) :]
        lowered = cleaned.lower()
    if _OPEN_TAG in lowered:  # unterminated block (output was cut off): drop the reasoning tail
        cleaned = cleaned[: lowered.find(_OPEN_TAG)]
    return cleaned.strip()


class ThinkStreamFilter:
    """Incrementally removes ``<think>...</think>`` blocks from streamed chunks.

    Tags may be split across chunks, so a short tail that could be the start of a tag is held back
    until the next chunk (or :meth:`flush`) decides what it is.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        """Add a chunk and return the text that is safe to emit."""
        self._buffer += chunk
        emitted: list[str] = []
        while self._buffer:
            tag = _CLOSE_TAG if self._inside else _OPEN_TAG
            index = self._buffer.lower().find(tag)
            if index >= 0:
                if not self._inside:
                    emitted.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(tag) :]
                self._inside = not self._inside
                continue
            keep = _partial_tag_suffix(self._buffer, tag)
            if not self._inside:
                emitted.append(self._buffer[: len(self._buffer) - keep])
            self._buffer = self._buffer[len(self._buffer) - keep :]
            break
        return "".join(emitted)

    def flush(self) -> str:
        """Return any held-back text at the end of the stream."""
        rest = "" if self._inside else self._buffer
        self._buffer = ""
        return rest


def _partial_tag_suffix(text: str, tag: str) -> int:
    """Length of the longest suffix of ``text`` that is a proper prefix of ``tag`` (case-insensitive)."""
    lowered = text.lower()
    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if tag.startswith(lowered[-size:]):
            return size
    return 0


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object found in ``text`` (after removing think blocks and code fences)."""
    cleaned = strip_think(text)
    candidates = [match.group(1) for match in _FENCE.finditer(cleaned)] + [cleaned]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for start in (i for i, char in enumerate(candidate) if char == "{"):
            try:
                value, _ = decoder.raw_decode(candidate, start)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return _unwrap_tool_call(value)
    return None


def _unwrap_tool_call(value: dict[str, Any]) -> dict[str, Any]:
    """Unwrap ``{"name": ..., "arguments": {...}}`` objects that models emit when imitating a tool call."""
    arguments = value.get("arguments") or value.get("parameters")
    if "name" in value and isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return value
        return parsed if isinstance(parsed, dict) else value
    return value


def json_instructions(schema: type[BaseModel]) -> str:
    """Compact, model-friendly description of the JSON object expected for ``schema``."""
    spec = schema.model_json_schema()
    definitions = spec.get("$defs", {})
    required = set(spec.get("required", []))
    lines = [
        "Reply with ONLY one JSON object (no markdown fences, no extra text) with these keys:",
    ]
    for name, prop in spec.get("properties", {}).items():
        kind = _describe_type(prop, definitions)
        note = prop.get("description", "")
        optional = "" if name in required else " (optional)"
        lines.append(f'- "{name}"{optional}: {kind}. {note}'.rstrip())
    return "\n".join(lines)


def _describe_type(prop: dict[str, Any], definitions: dict[str, Any]) -> str:
    """Render a JSON-schema property type as short text."""
    if "enum" in prop:
        return "one of " + ", ".join(json.dumps(value) for value in prop["enum"])
    if "anyOf" in prop:
        kinds = [_describe_type(option, definitions) for option in prop["anyOf"]]
        return " or ".join(dict.fromkeys(kinds))
    if "$ref" in prop:
        ref = definitions.get(prop["$ref"].rsplit("/", 1)[-1], {})
        fields = ", ".join(f'"{key}": string' for key in ref.get("properties", {}))
        return "{" + fields + "}"
    kind = prop.get("type", "string")
    if kind == "array":
        return "array of " + _describe_type(prop.get("items", {}), definitions)
    return {"null": "null", "number": "number", "integer": "integer", "boolean": "true/false"}.get(kind, "string")
