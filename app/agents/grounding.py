"""Numeric grounding. CLAUDE.md R1, enforced at the seam where prose is born.

    R1  "The database owns truth. The LLM owns language. No agent may assert a
         fact it did not read from a system of record. [...] If a value appears
         in a generated message, it was passed in as structured input, not
         produced by the model."

    R2  "An agent may explain a number. It may never produce one."

WHY A CHECK AND NOT A PROMPT
============================
"Only use figures from the input" is an instruction, and instructions are
followed most of the time. Most of the time is not a standard you can put a
trainer's payout behind: a model that writes "approximately 15,500" where the
engine computed 15,484 has produced a number, and the person reading it has no
way to tell which of the two it is. §12 asks for the assertion explicitly —
"compare every number in generated text against the structured input" — and this
module is that comparison, run inside the agent before a `Draft` is constructed,
not only inside a test.

The consequence of a violation is a raise, not a redaction. Silently stripping
the offending figure would leave a sentence like "the payout is  for July", and a
retry loop would hide a systematic problem behind an occasional extra call. The
draft is refused, the caller logs it, and a human sees an agent that failed
rather than a plausible paragraph with a wrong number in it.

WHAT COUNTS AS A NUMBER, AND THE TWO DELIBERATE EXCLUSIONS
==========================================================
Every digit run in the text is a candidate, including ones inside dates and
percentages, because "the period is 26-31 Jul" and "attendance is 92%" are both
factual claims R1 governs. Two things are excluded, each for a stated reason:

1. **Ordered-list markers** — a leading `1.` / `2)` on a line is layout, not a
   claim. Without this exclusion every bulleted draft fails, the check gets
   disabled, and R1 goes back to being a prompt.
2. **Nothing else.** In particular a bare year is *not* excluded. "in 2026" is a
   claim about a program that may run in 2025; if the caller wants the model to
   be able to write a year, the caller passes the year in.

Comparison is by numeric value, not by string, so `15,484`, `15484` and
`15484.00` are one value and the model may format as it likes. `Decimal` does the
normalising (R7 — never float, and this module handles money-shaped strings).
A percent sign is *not* stripped: `10%` and `10` are different claims, and a
caller that wants "10%" written passes `10` in and lets the model add the sign,
which it may, because the digits are grounded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

__all__ = [
    "GroundingViolation",
    "UngroundedFigureError",
    "assert_grounded",
    "collect_grounded_values",
    "figures_in",
]


#: A digit run, optionally with thousands separators and a decimal part. Written
#: to match how figures appear in Indian-format prose: `1,23,456.78` groups in
#: twos after the first three, so the separator pattern is deliberately loose.
_FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: A leading ordered-list marker: `1.` or `2)` at the start of a line. See the
#: module docstring for why this is the only exclusion.
_LIST_MARKER_RE = re.compile(r"^[ \t]*\d+[.)](?=\s)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class GroundingViolation:
    """One figure in generated text with no counterpart in the structured input."""

    figure: str
    value: Decimal

    def render(self) -> str:
        return f"{self.figure!r} (={self.value})"


class UngroundedFigureError(RuntimeError):
    """Generated text stated a figure that was not in the structured input.

    A `RuntimeError` rather than a `ValueError` for the reason given across this
    codebase: a broad `except ValueError` around parsing must not be able to
    swallow an R1 breach.

    Carries the violations so the caller can log precisely what was invented —
    which is the datum you want when deciding whether a prompt needs work or a
    model needs replacing.
    """

    def __init__(self, violations: Sequence[GroundingViolation], context: str) -> None:
        listed = ", ".join(v.render() for v in violations)
        super().__init__(
            f"{context}: generated text states {len(violations)} figure(s) that were not in "
            f"the structured input — {listed}. CLAUDE.md R1: the database owns truth, the "
            "LLM owns language; a value in a generated message must have arrived as "
            "structured input. R2: an agent may explain a number, never produce one. "
            "The draft is refused rather than corrected."
        )
        self.violations = tuple(violations)
        self.context = context


def _to_decimal(token: str) -> Decimal | None:
    """`'15,484'` -> `Decimal('15484')`. `None` when it is not a number."""
    cleaned = token.replace(",", "").rstrip(".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def figures_in(text: str) -> list[tuple[str, Decimal]]:
    """Every figure the text asserts, as `(as written, numeric value)`.

    Ordered-list markers are removed first, so `1. Deploy two trainers` claims
    nothing numeric while `1.5 days` claims 1.5.
    """
    scrubbed = _LIST_MARKER_RE.sub("", text)
    found: list[tuple[str, Decimal]] = []
    for match in _FIGURE_RE.finditer(scrubbed):
        value = _to_decimal(match.group())
        if value is not None:
            found.append((match.group(), value))
    return found


def collect_grounded_values(payload: object) -> set[Decimal]:
    """Every numeric value reachable in the structured input.

    Walks mappings, sequences and scalars, and — importantly — also scans *inside
    strings*. A snapshot field like `"26-31 Jul 2026"` or an invoice number like
    `"BCDP/26-27/JUL1"` legitimately licenses the model to write 26, 31, 2026 and
    27, because those digits came from the system of record. Refusing them would
    make the check unusable on exactly the fields agents most often quote.

    Booleans are skipped deliberately. `bool` is a subclass of `int` in Python, so
    without the guard a `signed=True` flag would silently ground the figure 1 —
    and "1 trainer", "1 day", "1 lakh" would all become quotable off the back of
    an unrelated boolean.
    """
    values: set[Decimal] = set()
    _walk(payload, values)
    return values


def _walk(node: object, into: set[Decimal]) -> None:
    if node is None or isinstance(node, bool):
        return
    if isinstance(node, int | Decimal):
        into.add(Decimal(node))
        return
    if isinstance(node, float):
        # Not raising: R7 governs money code, and a port may hand back a float
        # for something that is not money (a ratio, a score). Converting via
        # `str` rather than `Decimal(float)` avoids grounding the binary
        # expansion 0.1000000000000000055511151231257827 instead of 0.1.
        into.add(Decimal(str(node)))
        return
    if isinstance(node, str):
        for _, value in figures_in(node):
            into.add(value)
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            _walk(key, into)
            _walk(value, into)
        return
    if isinstance(node, Iterable):
        for item in node:
            _walk(item, into)
        return
    # Anything else (a UUID, a date, a snapshot dataclass) is stringified, which
    # routes it back through the string branch and picks up its digits.
    for _, value in figures_in(str(node)):
        into.add(value)


def assert_grounded(text: str, structured_input: object, context: str) -> None:
    """Raise `UngroundedFigureError` unless every figure in `text` came from input.

    `context` names the caller ("intake.draft_program") and lands in the error
    message, because the first question on seeing this failure is which agent
    invented what.

    Returns `None` on success. Like `check_transition()` in the approval state
    machine, it never returns a boolean: a falsy refusal is one forgotten `if`
    away from shipping an invented figure to a college.
    """
    allowed = collect_grounded_values(structured_input)
    violations = [
        GroundingViolation(figure=written, value=value)
        for written, value in figures_in(text)
        if value not in allowed
    ]
    if violations:
        raise UngroundedFigureError(violations, context)
