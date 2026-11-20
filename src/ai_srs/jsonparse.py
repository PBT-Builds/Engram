"""Forgiving JSON extraction from LLM replies.

Models wrap JSON in prose and code fences, or follow it with commentary that
itself contains braces. The old approach (a greedy `\\{.*\\}` regex) spanned
from the first `{` to the LAST `}` and failed on any trailing brace. Here we
strip code fences, then try `json.JSONDecoder.raw_decode` at each `{` / `[`
in order and return the first value that decodes — which tolerates text on
either side and skips a broken candidate to reach a good one.

Stdlib only.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_DECODER = json.JSONDecoder()
_FENCE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n?(.*?)```", re.DOTALL)


def _candidates_texts(raw: str):
    """The fenced block(s) first (most likely the intended payload), then the
    whole reply."""
    for m in _FENCE.finditer(raw):
        yield m.group(1)
    yield raw


def _scan(text: str, starts: str, want) -> Optional[Any]:
    for i, ch in enumerate(text):
        if ch not in starts:
            continue
        try:
            value, _ = _DECODER.raw_decode(text, i)
        except (json.JSONDecodeError, ValueError):
            continue
        if want is None or isinstance(value, want):
            return value
    return None


def _extract(raw, starts: str, want) -> Optional[Any]:
    if not raw or not isinstance(raw, str):
        return None
    for text in _candidates_texts(raw):
        stripped = text.strip()
        try:
            value = json.loads(stripped)
            if want is None or isinstance(value, want):
                if isinstance(value, (dict, list)):
                    return value
        except (json.JSONDecodeError, ValueError):
            pass
        value = _scan(text, starts, want)
        if value is not None:
            return value
    return None


def extract_object(raw) -> Optional[dict]:
    """The first JSON object in `raw`, or None."""
    return _extract(raw, "{", dict)


def extract_array(raw) -> Optional[list]:
    """The first JSON array in `raw`, or None."""
    return _extract(raw, "[", list)


def extract_value(raw) -> Optional[Any]:
    """The first JSON object OR array in `raw` (whichever comes first)."""
    return _extract(raw, "{[", None)
