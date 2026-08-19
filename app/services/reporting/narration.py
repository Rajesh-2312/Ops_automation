"""The prose half of Phase 6. CLAUDE.md §8 Reporting, ceiling Draft.

    R1  "The database owns truth. The LLM owns language."
    §2  OpenRouter is the sole gateway; route by task, not by default.
    §9  "Every answer cites source document and section. No citation → no answer."

WHAT THIS MODULE MAY DO
=======================
Turn a `GovernanceReport`, a `FeedbackSynthesis` or a `CollegeSummary` — objects
whose every figure came out of SQL — into paragraphs a Manager can edit. That is
all. It fetches nothing, computes nothing, and cannot state a number that was not
on the object it was handed.

NOTHING HERE RELEASES ANYTHING (R3, R4)
=======================================
There is no send, no queue push, no state transition. The output is a `Narration`
— a string and its telemetry. `drafts.py` binds it to a DRAFT artifact and a
human takes it from there. §8's ceiling for Reporting is Draft, and the way that
is made structural is that this module has no function capable of anything else.

REUSE, NOT REIMPLEMENTATION — THE TWO CHECKS
============================================
Both gates already exist in this codebase and are used here rather than rewritten:

* **Figures** — `app.agents.grounding.assert_grounded`. Every digit run in the
  generated text must appear in the structured input, compared by numeric value
  so the model may reformat `15,484` as it likes. A violation RAISES; the draft is
  refused rather than repaired, for the reason that module states at length — a
  silently stripped figure leaves a sentence with a hole in it, and a retry loop
  hides a systematic problem behind an occasional extra call.

* **Citations** — `app.rag.guards.check_citations`. Used only when the caller
  supplied retrieved passages, because §9's rule binds retrieval-backed claims. A
  narration written purely from SQL facts cites nothing and needs no `[n]` markers;
  demanding them would train the model to invent them.

`app.rag.guards.check_figures` is deliberately NOT also run. It is the same rule
as `assert_grounded` expressed for the Copilot's inputs, and running both would
mean two definitions of "grounded" that can disagree. Figures are governed by
`grounding`, citations by `guards`, and each has exactly one owner.

WHY THE LLM ARRIVES AS A PROTOCOL AND NOT AS `LLMClient`
=======================================================
`Completer` mirrors the single method of `app.core.llm.LLMClient` this module
uses, so a test can supply a canned response without an API key. `LLMClient`
remains the only implementation and OpenRouter the sole gateway (§2). Note what
is absent: no `model` parameter anywhere in this file. Routing is by `LLMTask`
through `TASK_TIER`, so no model id is ever hardcoded — a governance report goes
to the frontier tier because `LLMTask.GOVERNANCE_REPORT` is mapped there, not
because this module picked a model.

The protocol is redeclared here rather than imported from
`app.agents.runtime.Completer` to keep `app/services/` from depending on
`app/agents/`. It is three lines of structural type; the alternative is a service
layer that cannot be used without the agent layer being importable.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import structlog

from app.agents.grounding import assert_grounded
from app.core.llm import LLMResponse
from app.domain.enums import LLMTask
from app.rag.guards import Refusal, check_citations
from app.services.reporting.assembly import (
    CollegeSummary,
    FeedbackSynthesis,
    GovernanceReport,
)

__all__ = [
    "Citation",
    "Completer",
    "Narration",
    "NarrationRefused",
    "ReportNarrator",
]

_log = structlog.get_logger(__name__)


class Completer(Protocol):
    """The one `app.core.llm.LLMClient` method this service uses. §2's gateway."""

    async def complete(
        self,
        task: LLMTask,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class Citation:
    """One retrieved passage a narration may lean on. §9: no citation, no answer.

    Both `document_title` and `section` are required, so a passage that cannot be
    cited cannot be constructed — the same constraint `app.agents.ports`
    `RetrievedPassage` imposes, and for the same reason. There is no numeric field:
    §9 forbids structured facts coming from retrieval at all, so a passage supplies
    policy and context, never a figure the report then quotes.
    """

    document_title: str
    section: str
    text: str

    def render(self, index: int) -> str:
        return f"[{index}] {self.document_title} — {self.section}\n{self.text}"


@dataclass(frozen=True, slots=True)
class Narration:
    """Generated prose plus the §11 record of what it cost to generate it.

    Returned rather than merely logged so a test can assert the record exists and
    an operator can see report generation cost without parsing log lines. The
    prompt is recorded as a character count, not verbatim: a governance prompt
    carries trainer names and college detail (§4), and the full text goes to the
    debug-level log inside `app.core.llm` behind the same gate as everything else.
    """

    body: str
    task: LLMTask
    model: str
    prompt_chars: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    citations: tuple[Citation, ...] = ()
    at: dt.datetime = dt.datetime.min

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class NarrationRefused(RuntimeError):
    """A narration failed §9 and was discarded rather than repaired.

    A `RuntimeError` and not a `ValueError`, matching `UngroundedFigureError`: a
    broad `except ValueError` around request parsing must not be able to swallow
    "this report cited a source that does not exist".

    Carries the `Refusal` so the caller can log the reason code — the proportion of
    reports refused as `invalid_citation` is a fact about the model worth watching.
    """

    def __init__(self, refusal: Refusal, context: str) -> None:
        super().__init__(f"{context}: {refusal.reason.value} — {refusal.message}")
        self.refusal = refusal
        self.context = context


# --- prompts ------------------------------------------------------------------
#
# Every prompt states the same three rules, because the model does not read
# CLAUDE.md and the checks that enforce them raise rather than correct. Asking
# first makes the raise rare; the raise is what makes the asking trustworthy.

_RULES: Final[str] = (
    "Rules you must follow exactly:\n"
    "1. Use ONLY the figures, dates, names and counts in the structured data given "
    "to you. State no number that is not there. Do not add, average, total, "
    "estimate or round anything.\n"
    "2. A figure you cannot find in the data does not go in the text. Say the data "
    "does not record it.\n"
    "3. Do not describe anything as approved, sent, shared or agreed. A human will "
    "review, edit and send this.\n"
)

_GOVERNANCE_SYSTEM: Final[str] = (
    "You write the narrative of a governance report for byteXL's operations team, "
    "covering one college training programme over one reporting period. The report "
    "is reviewed internally and may then be shared with the college.\n"
    "\n" + _RULES + "4. Structure: delivery, attendance, assessments, feedback, and "
    "open risks. Lead with what happened, not with how it is going.\n"
    "5. Name incomplete trainer tracksheets explicitly as an operational gap — they "
    "block the payout cycle.\n"
    "6. Neutral and factual. No congratulation, no marketing language."
)

_FEEDBACK_SYSTEM: Final[str] = (
    "You summarise collected feedback for a byteXL operations manager.\n"
    "\n" + _RULES + "4. The average, the range and the response counts are given to "
    "you. Explain what they show; do not recompute them or infer a trend from a "
    "single collection.\n"
    "5. Say plainly how many collections carried no score. An average taken over "
    "part of the data must be presented as such.\n"
    "6. Six sentences at most."
)

_COLLEGE_SYSTEM: Final[str] = (
    "You write a short internal status summary of every training programme running "
    "at one college, for a byteXL operations manager.\n"
    "\n" + _RULES + "4. One short paragraph per programme, in the order given.\n"
    "5. Lead each with the programme's stage and what is outstanding.\n"
    "6. This summary contains no commercial information. Do not refer to rates, "
    "costs, payouts or invoices, and do not speculate about them."
)

_CITATION_INSTRUCTION: Final[str] = (
    "\n\nSOURCE DOCUMENTS\n"
    "Policy statements you make must cite a numbered source below with a marker "
    "like [1]. Cite only these sources and only by their given number. Never take a "
    "figure from a source document — figures come from the structured data only.\n"
)


@dataclass(frozen=True, slots=True)
class ReportNarrator:
    """Writes the prose for the three §8 Reporting outputs. Draft ceiling only.

    Frozen and holding nothing but the gateway: a narrator with mutable state
    would be a place for facts from one report to survive into the next, and the
    whole guarantee of this layer is that the only facts in scope are the ones
    passed to the call.
    """

    llm: Completer
    #: Low but not zero. A report regenerated for the same period should read
    #: substantially the same; the figures are pinned by the grounding check
    #: either way, so this governs wording rather than truth.
    temperature: float = 0.2

    async def narrate_governance(
        self,
        report: GovernanceReport,
        *,
        passages: Sequence[Citation] = (),
    ) -> Narration:
        """The governance narrative. Frontier tier via `LLMTask.GOVERNANCE_REPORT`.

        §2 routes this task to the frontier model and this is the task it exists
        for: a governance report is read by a college and a wrong emphasis is
        expensive to retract.

        The structured input is `report.as_payload()` — the same mapping
        `drafts.py` freezes into the artifact (R4). One object, so the prose can
        only quote what would be approved.
        """
        return await self._narrate(
            LLMTask.GOVERNANCE_REPORT,
            system=_GOVERNANCE_SYSTEM,
            payload=report.as_payload(),
            context="reporting.narrate_governance",
            passages=passages,
        )

    async def narrate_feedback(
        self,
        synthesis: FeedbackSynthesis,
        *,
        program_name: str,
        passages: Sequence[Citation] = (),
    ) -> Narration:
        """Feedback synthesis prose. Volume tier — it is a summary (§2).

        `program_name` travels in the payload rather than in the system prompt so
        it is part of the grounded input: a programme name containing a year
        ("bCAP 2026") licenses the model to write 2026, and putting it in the
        prompt instead would make that a grounding violation.
        """
        payload = {"program_name": program_name, **synthesis.as_payload()}
        return await self._narrate(
            LLMTask.SUMMARY,
            system=_FEEDBACK_SYSTEM,
            payload=payload,
            context="reporting.narrate_feedback",
            passages=passages,
        )

    async def narrate_college_summary(
        self,
        summary: CollegeSummary,
        *,
        passages: Sequence[Citation] = (),
    ) -> Narration:
        """College summary prose. Volume tier. Carries no commercials by type."""
        return await self._narrate(
            LLMTask.SUMMARY,
            system=_COLLEGE_SYSTEM,
            payload=summary.as_payload(),
            context="reporting.narrate_college_summary",
            passages=passages,
        )

    async def _narrate(
        self,
        task: LLMTask,
        *,
        system: str,
        payload: object,
        context: str,
        passages: Sequence[Citation],
    ) -> Narration:
        """One grounded, cited, logged completion. The only way prose is produced.

        Order matters and is not incidental:

        1. Call the model.
        2. Log the invocation (§11) — including when the checks below then reject
           the output, because a refused generation still cost tokens and is the
           most interesting line in the log.
        3. `check_citations` when sources were supplied (§9).
        4. `assert_grounded` (R1) — last, and raising, so no caller can obtain
           ungrounded prose by ignoring a return value.
        """
        user = _user_prompt(payload, passages)
        full_system = system + (_CITATION_INSTRUCTION if passages else "")
        response = await self.llm.complete(
            task,
            system=full_system,
            user=user,
            temperature=self.temperature,
        )

        prompt_chars = len(full_system) + len(user)
        # §11: prompt, tokens, latency, every invocation. No tools are called from
        # this layer — the caller did the reading — so there is no tool list.
        _log.info(
            "reporting.narration",
            context=context,
            task=task.value,
            model=response.model,
            tier=response.tier.value,
            prompt_chars=prompt_chars,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
            cited_sources=len(passages),
        )

        if passages:
            refusal = check_citations(response.text, len(passages))
            if refusal is not None:
                _log.warning(
                    "reporting.narration_refused",
                    context=context,
                    reason=refusal.reason.value,
                )
                raise NarrationRefused(refusal, context)

        # R1, checked before the caller can do anything with the text.
        assert_grounded(response.text, payload, context)

        return Narration(
            body=response.text,
            task=task,
            model=response.model,
            prompt_chars=prompt_chars,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
            citations=tuple(passages),
            at=dt.datetime.now(dt.UTC),
        )


def _user_prompt(payload: object, passages: Sequence[Citation]) -> str:
    """The structured facts as JSON, then the numbered sources.

    JSON rather than prose, because the model must be able to tell a figure it may
    quote from a sentence it may paraphrase, and because the same serialisation is
    what `assert_grounded` walks. Amounts are already strings on every
    `as_payload()` in `assembly.py` (R7), so nothing here can render a rupee as a
    float.
    """
    blocks = [
        "STRUCTURED DATA (the only source of figures — CLAUDE.md R1)",
        json.dumps(payload, indent=2, sort_keys=True, default=str),
    ]
    if passages:
        blocks.append("SOURCES (policy and context only — never figures)")
        blocks.extend(passage.render(i) for i, passage in enumerate(passages, start=1))
    return "\n\n".join(blocks)
