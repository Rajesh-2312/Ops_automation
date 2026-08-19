"""Reading structured output back from a model. Small, strict, shared.

An extraction agent asks for JSON and gets JSON *plus* whatever the model felt
like saying around it — a fenced code block, a "Here's the extraction:" preamble,
an apology. Every agent that extracts needs the same tolerant-then-strict parse,
and writing it twice guarantees the two copies diverge in their edge cases.

Strictness where it matters: the result must be a JSON **object**. A model that
returns a bare list or a string has not answered the question that was asked, and
coercing it into a shape ("wrap it in a dict") produces a plausible payload with
no relationship to the document. That becomes a Program record somebody signs a
work order against, so it fails instead.
"""

from __future__ import annotations

import json
import re
from typing import Final

from pydantic import JsonValue

__all__ = ["MalformedModelOutputError", "parse_json_object"]

#: A fenced block, with or without a language tag. Non-greedy so the first block
#: wins — a model that emits two has already gone off-script and the first is the
#: one it was asked for.
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"```(?:json|JSON)?\s*(?P<body>.*?)```", re.DOTALL)


class MalformedModelOutputError(RuntimeError):
    """The model's structured output could not be read as a JSON object.

    A `RuntimeError`, not a `ValueError`, for the reason this codebase gives
    elsewhere: a broad `except ValueError` around form parsing must not swallow
    "the extraction agent returned something unusable" — that failure needs to
    reach a human, because the alternative is an empty Program draft that looks
    like a real one.
    """

    def __init__(self, context: str, detail: str, raw: str) -> None:
        excerpt = raw.strip()[:200]
        super().__init__(
            f"{context}: model output is not a JSON object ({detail}). "
            f"First 200 characters: {excerpt!r}"
        )
        self.context = context
        self.raw = raw


def parse_json_object(text: str, context: str) -> dict[str, JsonValue]:
    """Read a JSON object out of a model response. Raises if it is not one.

    Tries the fenced block first, then the raw text, then the widest
    brace-to-brace span — in that order, because each is a weaker signal about
    what the model meant and the strongest available reading should win.
    """
    candidates: list[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group("body"))
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    detail = "no JSON found"
    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            detail = f"invalid JSON: {exc.msg}"
            continue
        if not isinstance(parsed, dict):
            detail = f"parsed a {type(parsed).__name__}, expected an object"
            continue
        return parsed
    raise MalformedModelOutputError(context, detail, text)
