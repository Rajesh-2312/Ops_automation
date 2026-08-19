"""The review surface. CLAUDE.md §8:

    "Comms Service — single outbound queue. Channel, recipient, template, and
     diff-from-template shown at approval."

The diff is the load-bearing half of that sentence. An approver asked to re-read
a whole message reads the first two and approves the rest; an approver shown
"three lines differ from the template, here they are" is reviewing the thing that
actually carries risk — what the drafter, usually a model, changed.

THE TWO-STEP THAT MAKES THE DIFF MEAN ANYTHING
==============================================
A naive diff of raw template against final message would report every substituted
value as a change: `{{trainer_name}}` becomes "VEMA PRUDHVI SAI" and the approver
is asked to review a fact that came out of a SQL query. That is noise, and noise
in a review surface is how a review becomes a rubber stamp.

So there are two steps, and only the second is the diff:

1. `render(template, values)` substitutes the structured facts, producing the
   BASELINE — what the template alone says about this program, this trainer, this
   month. CLAUDE.md R1 lives in this step: "If a value appears in a generated
   message, it was passed in as structured input, not produced by the model", and
   `values` is that input, persisted alongside the message in
   `comms_messages.template_values`.
2. `diff_from_template(baseline, body)` compares the baseline with what is
   actually queued. Everything it reports is drafter-authored prose. A message
   that is pure substitution diffs to `identical`, and an approver can clear it
   in one glance, correctly.

The whole module is pure — no I/O, no database, no LLM. It takes strings and
returns a dataclass, which is what lets `tests/unit/test_comms_diff.py` pin the
behaviour without either.

WHY LINES, AND WHY THE HUNKS CARRY BOTH SIDES
=============================================
`difflib.SequenceMatcher` over lines, not words or characters. A comms message is
paragraphs, an approver reasons in sentences, and a word-level diff of a rewritten
paragraph produces a shredded interleaving that is harder to read than the two
paragraphs side by side.

Each hunk carries the template lines AND the message lines it replaced them with,
rather than a unified `-`/`+` stream, because the consumer is a review UI and a
JSON column rather than a terminal. Rendering a unified view from both sides is
trivial; recovering the two sides from a unified string is not.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Final

from pydantic import JsonValue

__all__ = [
    "DIFF_VERSION",
    "PLACEHOLDER_RE",
    "DiffOp",
    "Hunk",
    "TemplateDiff",
    "TemplateRenderError",
    "UnrenderableValueError",
    "diff_from_template",
    "render",
]

#: Shape of the stored `comms_messages.diff` object. Stamped into every diff so a
#: row rendered by an older algorithm is identifiable rather than silently
#: reinterpreted — the same reason `app.services.approval.hashing` carries a
#: canonicalisation version. Bump it when the hunk shape changes, never when a
#: cosmetic detail does.
DIFF_VERSION: Final[int] = 1

#: `{{key}}`, with optional inner whitespace. Deliberately not a general template
#: language: no conditionals, no loops, no expressions. A template that can
#: compute is a template that can compute money, and R2 puts every rupee in
#: `services/remuneration/engine.py`.
PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class TemplateRenderError(Exception):
    """A template that cannot be rendered from the values it was given.

    A plain `Exception`, like `app.services.approval.hashing.CanonicalisationError`
    and for the same reason: this is a governance failure — a message with an
    unfilled slot would go to a college reading `{{college_name}}` — and it must
    not be swallowed by a broad `except ValueError` written for form parsing.
    """


class UnrenderableValueError(TemplateRenderError):
    """A value that must not be interpolated into a message.

    Chiefly `float` (R7). A rupee amount that arrives as `15584.000000000001`
    prints that way in a message a trainer reads, and the message is then wrong
    in the one way nobody forgives. `Decimal` and `int` are accepted; a float is
    refused at the boundary rather than rounded quietly here.
    """


class DiffOp(StrEnum):
    """What one hunk does to the baseline.

    `difflib`'s opcodes minus `equal` — unchanged regions are not hunks, they are
    the absence of one. Kept as an enum rather than the raw strings `difflib`
    returns so that §11 holds for the value stored in `comms_messages.diff`.
    """

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class Hunk:
    """One divergence between the rendered template and the queued message.

    `template` and `message` are both present on every hunk, and one of them is
    empty for a pure addition or removal. `at` is the line index in the BASELINE,
    so hunks sort into template order and a UI can interleave them with the
    unchanged text without a second pass.
    """

    op: DiffOp
    at: int
    template: tuple[str, ...]
    message: tuple[str, ...]

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "op": self.op.value,
            "at": self.at,
            "template": list(self.template),
            "message": list(self.message),
        }


@dataclass(frozen=True, slots=True)
class TemplateDiff:
    """The whole review surface for one message.

    `identical` is the field the approval UI should lead with. A message that is
    pure substitution is a message where the only judgement left is "is this the
    right recipient", and saying so in one boolean is worth more than a hunk list
    the reader has to scan to discover it is empty.

    Immutable, and rendered to JSON by `as_json()` for
    `comms_messages.diff`. That column is frozen by the content hash at approval,
    so what the approver was shown is recoverable exactly (R4).
    """

    hunks: tuple[Hunk, ...]
    lines_added: int
    lines_removed: int
    template_lines: int
    message_lines: int

    @property
    def identical(self) -> bool:
        """True when the message is the rendered template, unchanged.

        Not `not self.hunks` at the call site, because that reads as "no hunks"
        and the question being asked is "did the drafter write anything of its
        own". Same value, and the name is the documentation.
        """
        return not self.hunks

    @property
    def lines_changed(self) -> int:
        """Total lines the approver has to read. Added plus removed, because a
        rewritten line is both — it is not one line of work, it is two."""
        return self.lines_added + self.lines_removed

    def as_json(self) -> dict[str, JsonValue]:
        """The `comms_messages.diff` payload.

        `version` first and always. A stored diff outlives the code that made it,
        and a consumer that cannot tell which algorithm produced a row will
        eventually misread one.
        """
        return {
            "version": DIFF_VERSION,
            "identical": self.identical,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "template_lines": self.template_lines,
            "message_lines": self.message_lines,
            "hunks": [hunk.as_json() for hunk in self.hunks],
        }


# --- step 1: render the baseline ----------------------------------------------


def _as_text(key: str, value: object) -> str:
    """One structured value as it appears in a message.

    R7 is enforced here rather than trusted: a `float` is refused outright, and
    `Decimal` is rendered without exponent notation so a large amount never
    reaches a trainer as `1.5584E+4`. Dates go out ISO — the presentation layer
    owns IST formatting (§11), and this function is not it.
    """
    if isinstance(value, bool):
        # Before the int branch: bool IS an int in Python, and "True" is not a
        # sentence anybody wants to read in a message.
        return "yes" if value else "no"
    if isinstance(value, float):
        raise UnrenderableValueError(
            f"template value {key!r} is a float. CLAUDE.md R7: Decimal only, no exceptions — "
            "a rupee amount that renders as 15584.000000000001 is wrong in the one way "
            "nobody forgives. Pass a Decimal or a pre-formatted string."
        )
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if value is None:
        raise TemplateRenderError(
            f"template value {key!r} is None. A slot with no value is an unanswered "
            "question, not an empty string — supply the fact or use a template that "
            "does not ask for it."
        )
    return str(value)


def render(template: str, values: Mapping[str, object]) -> str:
    """Substitute `{{key}}` slots and return the BASELINE (step 1 above).

    Every placeholder must have a value. A missing one raises rather than
    rendering an empty string or leaving the braces in place: both failure modes
    reach the recipient looking like a bug in byteXL, and both are invisible in a
    diff, because the diff compares the baseline with the message and a slot
    missing from BOTH sides is not a divergence.

    Extra keys in `values` are permitted and deliberately so. `values` is also the
    R1 provenance record persisted in `comms_messages.template_values`, and a
    fact that was fetched, considered and not printed is worth keeping — it is how
    an auditor sees what the drafter had in front of it.

    Pure and total: no I/O, no clock, no model. Given the same template and the
    same values it returns the same string, which is what lets the baseline be
    hashed as part of the R4 freeze.
    """
    missing: list[str] = []

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return _as_text(key, values[key])

    rendered = PLACEHOLDER_RE.sub(_substitute, template)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise TemplateRenderError(
            f"template placeholders have no value: {names}. A message must not go to an "
            "approver with an unfilled slot — the braces survive into what the recipient "
            "reads, and a blank reads as a fact nobody has. Supply the value from a system "
            "of record (CLAUDE.md R1) or use a template that does not ask for it."
        )
    return rendered


# --- step 2: diff the baseline against what is queued -------------------------


def _lines(text: str) -> list[str]:
    """Split for comparison, ignoring trailing whitespace on each line.

    Trailing spaces are invisible to the approver, so a hunk caused by one is a
    hunk that cannot be understood. The stored `body` keeps whatever it had —
    this normalisation is for COMPARISON only and never for what is sent.
    """
    return [line.rstrip() for line in text.splitlines()]


def diff_from_template(template_body: str, body: str) -> TemplateDiff:
    """The §8 diff-from-template, between a rendered baseline and the message.

    `template_body` is the output of `render()`, not the raw template — passing
    the raw template here would report every substituted fact as drafter prose
    and defeat the whole point (see the module docstring).

    Pure. Deterministic. Symmetric in nothing: the first argument is the baseline
    and the second is what would go out, and swapping them inverts every hunk.
    """
    template_lines = _lines(template_body)
    message_lines = _lines(body)

    hunks: list[Hunk] = []
    added = 0
    removed = 0

    matcher = SequenceMatcher(a=template_lines, b=message_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        left = tuple(template_lines[i1:i2])
        right = tuple(message_lines[j1:j2])
        removed += len(left)
        added += len(right)
        if tag == "insert":
            op = DiffOp.ADDED
        elif tag == "delete":
            op = DiffOp.REMOVED
        else:
            op = DiffOp.CHANGED
        hunks.append(Hunk(op=op, at=i1, template=left, message=right))

    return TemplateDiff(
        hunks=tuple(hunks),
        lines_added=added,
        lines_removed=removed,
        template_lines=len(template_lines),
        message_lines=len(message_lines),
    )
