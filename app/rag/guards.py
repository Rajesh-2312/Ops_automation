"""The two refusals that make the Ops Copilot safe to trust.

Both are CODE PATHS, not prompt instructions. CLAUDE.md R3 states the principle
for tool binding — "enforced by tool binding, not by prompt instruction" — and it
applies just as directly here: a system prompt asking a model to cite its sources
and to decline numeric questions is a request, and requests are honoured most of
the time. Most of the time is not a permission model.

REFUSAL 1 — STRUCTURED FACTS (R1, §9)
-------------------------------------
    "Structured facts (dates, amounts, counts) are **never** retrieved from RAG.
     Query the database. RAG supplies policy and context only."

So "how many days did this trainer work in July?" is refused BEFORE retrieval and
before any model sees the question. Not answered cautiously, not answered with a
hedge — refused, with a message naming where the answer actually lives. The
failure this prevents is specific and severe: a payout dispute settled from a
sentence in a six-week-old status report that happened to mention a number.

REFUSAL 2 — UNCITED ANSWERS (§9)
--------------------------------
    "Every answer cites source document and section. No citation → no answer."

An answer whose `[n]` markers do not resolve to chunks that were actually
retrieved is discarded and a refusal is returned in its place. Discarded rather
than repaired: an answer with an invented citation is not an answer with a small
formatting problem, it is a fabrication that reads as sourced.

REFUSAL 3 — FABRICATED FIGURES (R1, R2)
---------------------------------------
    "No agent may assert a fact it did not read from a system of record."
    "An agent may explain a number. It may never produce one."

Every numeric token in a generated answer must appear verbatim in the retrieved
context, in the question, or in the structured facts the CALLER passed in from
SQL. Anything else — an arithmetic result, a rounded restatement, a plausible
guess — fails the check and the answer is refused. This is the reason the copilot
may safely "explain a figure it was given": the check proves the figure was
given.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# --- refusal vocabulary -------------------------------------------------------


class RefusalReason(StrEnum):
    """Why the Copilot declined. A closed vocabulary, because these are counted.

    The proportion of questions refused as STRUCTURED_FACT is the single most
    useful operating metric this system has: it is a live list of the SQL
    endpoints the platform still owes its users. A free-text reason would make
    that unmeasurable.
    """

    #: The question asks for a date, an amount or a count. §9 sends it to SQL.
    STRUCTURED_FACT = "structured_fact"
    #: Nothing in the permitted corpora was close enough to cite.
    NO_SOURCES = "no_sources"
    #: The model produced prose with no resolvable citation. §9: no citation, no answer.
    UNCITED = "uncited"
    #: The model cited a source that was not retrieved.
    INVALID_CITATION = "invalid_citation"
    #: The model produced a number that is in none of its inputs. R1, R2.
    FABRICATED_FIGURE = "fabricated_figure"
    #: The persona holds no corpus at all (trainer, college). §4.
    NO_CORPUS_ACCESS = "no_corpus_access"


@dataclass(frozen=True, slots=True)
class Refusal:
    """A declined answer. Carries what the user should do instead.

    `message` is written for the person who asked, not for a log. A refusal that
    says only "unable to answer" trains people to stop asking; one that says
    "attendance counts come from the tracksheet, not from documents — open the
    trainer's attendance view" teaches the boundary.
    """

    reason: RefusalReason
    message: str


# --- refusal 1: structured facts ----------------------------------------------
#
# Two independent signals, either sufficient. Kept as explicit, greppable
# patterns rather than as a model call, because a classifier that is itself an
# LLM makes R1's enforcement depend on the thing R1 distrusts.
#
# The bias is deliberately towards refusing. A false refusal costs one round trip
# and a message naming the right tool; a false acceptance puts an unverifiable
# number in front of someone about to approve a payout. That asymmetry is the
# whole of §9's "query the database" rule.

#: Interrogatives that ask for a quantity, a date or a count.
_QUANTITY_INTERROGATIVE_RE: Final[re.Pattern[str]] = re.compile(r"""(?ix)
    \b(
        how\s+(many|much|long|often)
      | what(?:'s|\s+is|\s+was|\s+are|\s+were)?\s+the\s+
        (total|sum|amount|number|count|balance|rate|figure)
      | when\s+(did|was|is|will|does)
      | what\s+date
      | which\s+date
      | on\s+what\s+date
      | total\s+(of|for)
      | number\s+of
      | count\s+of
    )\b
    """)

#: Nouns that ARE a stored figure. Asking for one of these is asking the database
#: a question, whatever the sentence around it looks like.
_FACT_NOUN_RE: Final[re.Pattern[str]] = re.compile(r"""(?ix)
    \b(
        net\s+pay | gross\s+pay | payable\s+days | days\s+worked | tds(\s+amount)?
      | invoice\s+(number|amount) | payout\s+amount | attendance\s+(count|percentage|percent)
      | syllabus\s+(percentage|percent|completion) | outstanding\s+balance
      | amount\s+(paid|due|payable) | headcount | student\s+count
    )\b
    """)

#: A currency figure in the question ("is ₹65,000 right?") is a question ABOUT a
#: number the asker already has, which the Copilot may legitimately explain. It
#: is therefore NOT on its own a structured-fact signal — only the two patterns
#: above are. Recorded here because the omission looks like an oversight.

#: The one exemption, and it is essential rather than a softening. "How are
#: payable days counted for a bCAP trainer?" contains a fact noun and is the
#: single most useful question this Copilot can answer — it is §5's central
#: branch, it is pure policy, and the answer is in the SOP rather than in any
#: row. Without this pattern the fact-noun signal would refuse exactly the
#: questions the corpus exists for, and a Copilot that refuses its own best use
#: case is one people stop opening.
#:
#: It exempts only the FACT NOUN signal, never the quantity interrogative: "how
#: much is the net pay" asks for a value however it is phrased, and no wording
#: makes that a policy question.
_POLICY_INTENT_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?ix)
    \b(how|what|which|why|explain|describe)\b .{0,80}?
    \b(
        counted | calculated | computed | determined | derived | defined
      | treated | handled | works? | governed | approved | validated
      | process | procedure | policy | rules? | sop | semantics | basis
    )\b
    """,
    re.DOTALL,
)


def is_structured_fact_question(question: str) -> bool:
    """True when the question asks for a date, an amount or a count (§9, R1).

    Deliberately syntactic and deliberately over-inclusive. See the block comment
    above on why the asymmetry runs this way, and `_POLICY_INTENT_RE` for the one
    exemption and why it is not a softening.
    """
    if _QUANTITY_INTERROGATIVE_RE.search(question):
        return True
    return bool(_FACT_NOUN_RE.search(question)) and not _POLICY_INTENT_RE.search(question)


def structured_fact_refusal(question: str) -> Refusal:
    """The refusal returned for a numeric question. Names where the answer lives."""
    return Refusal(
        reason=RefusalReason.STRUCTURED_FACT,
        message=(
            "That question asks for a stored figure — a date, an amount or a count — "
            "and the Copilot answers only from policy documents. A number read out of "
            "a document is not the number of record and may be months stale "
            "(CLAUDE.md R1, §9). Attendance, payable days, payouts and invoice details "
            "come from the tracksheet and the remuneration views; ask there, or ask me "
            "what the POLICY is instead — for example, 'how are payable days counted "
            "for a bCAP trainer?'"
        ),
    )


# --- refusal 2: citations -----------------------------------------------------

#: `[1]`, `[2]`, and `[1, 3]` / `[1][3]` by repetition. One bracketed integer per
#: match; the answer template asks for exactly this form.
_CITATION_RE: Final[re.Pattern[str]] = re.compile(r"\[(\d{1,2})\]")


def extract_citations(answer: str) -> tuple[int, ...]:
    """Every `[n]` marker in the answer, in order of appearance, deduplicated."""
    seen: list[int] = []
    for match in _CITATION_RE.finditer(answer):
        value = int(match.group(1))
        if value not in seen:
            seen.append(value)
    return tuple(seen)


def check_citations(answer: str, source_count: int) -> Refusal | None:
    """§9's citation rule, as a gate. Returns a `Refusal`, or None when clean.

    Two failures, kept distinct because they mean different things about the
    model: NO markers at all is a formatting miss, while a marker pointing at a
    source that was never supplied is a fabrication. Both refuse; only the second
    is worth paging someone about if it becomes common.
    """
    markers = extract_citations(answer)
    if not markers:
        return Refusal(
            reason=RefusalReason.UNCITED,
            message=(
                "The Copilot produced an answer it could not attribute to a source "
                "document and section, so the answer was discarded (CLAUDE.md §9: "
                "no citation, no answer). Try rephrasing the question, or consult the "
                "SOP directly."
            ),
        )
    out_of_range = [m for m in markers if m < 1 or m > source_count]
    if out_of_range:
        return Refusal(
            reason=RefusalReason.INVALID_CITATION,
            message=(
                "The Copilot cited a source that was not retrieved, so the answer was "
                "discarded (CLAUDE.md §9). This is a fabricated citation, not a "
                "formatting problem, and it has been logged."
            ),
        )
    return None


# --- refusal 3: fabricated figures --------------------------------------------

#: Any run of digits, with optional grouping separators and decimals. Matches
#: `26`, `65,000`, `15484.00`, `0.10`.
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _digits(token: str) -> str:
    """`65,000` and `65000` are the same figure; `65,000.00` normalises too.

    Grouping separators and a trailing `.00` are presentation. Comparing on the
    digits alone means a model that reformats a figure it was given is not
    accused of inventing it, while a model that computes a NEW figure still is —
    because the digits of a computed number appear nowhere in the input.
    """
    cleaned = token.replace(",", "")
    if "." in cleaned:
        whole, _, frac = cleaned.partition(".")
        frac = frac.rstrip("0")
        return f"{whole}.{frac}" if frac else whole
    return cleaned


def _grounded_figures(sources: Iterable[str]) -> frozenset[str]:
    """Every figure that appears in the inputs, normalised."""
    return frozenset(
        _digits(match.group(0)) for source in sources for match in _NUMBER_RE.finditer(source)
    )


def check_figures(
    answer: str,
    *,
    question: str,
    contexts: Sequence[str],
    facts: Mapping[str, str] | None = None,
) -> Refusal | None:
    """R1 and R2, as a gate: every figure in the answer must have come from an input.

    Citation markers are removed before the scan — `[2]` is a reference, not a
    figure, and leaving it in would make every properly-cited answer fail.

    The permitted inputs are exactly three, and the third is the important one:

      * the retrieved chunk text (what the corpus said),
      * the question (the asker's own figure, which the Copilot may discuss),
      * `facts` — values the CALLER read from the database and passed in.

    `facts` is what makes §9's "hybrid answers must visibly separate the two"
    workable: structured values enter as structured input, are rendered in their
    own block by `app/rag/copilot.py`, and are legal for the prose to reference
    precisely BECAUSE they came from SQL. Nothing the model derives on its own
    survives this check — an arithmetic result has digits that appear in no input,
    which is R2 enforced rather than requested.
    """
    scrubbed = _CITATION_RE.sub(" ", answer)
    grounded = _grounded_figures([question, *contexts, *(facts.values() if facts else ())])
    ungrounded = sorted(
        {
            _digits(match.group(0))
            for match in _NUMBER_RE.finditer(scrubbed)
            if _digits(match.group(0)) not in grounded
        }
    )
    if not ungrounded:
        return None
    return Refusal(
        reason=RefusalReason.FABRICATED_FIGURE,
        message=(
            "The Copilot's answer contained figures that appear in none of its sources "
            f"({', '.join(ungrounded[:5])}), so the answer was discarded. An agent may "
            "explain a number; it may never produce one (CLAUDE.md R1, R2). Query the "
            "system of record for the figure and ask again."
        ),
    )
