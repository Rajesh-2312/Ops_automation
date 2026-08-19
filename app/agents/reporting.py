"""Reporting agent. CLAUDE.md §8: "Governance report, feedback synthesis,
college summaries". Ceiling: Draft.

WHAT THIS AGENT IS, GIVEN THAT `app/services/reporting/` ALREADY EXISTS
=======================================================================
Phase 6 shipped the reporting *service*: `assembly.py` folds SQL rows into the
sections a report is made of, `narration.py` writes the prose, `drafts.py` binds
the result to an R4 DRAFT artifact. That service is the API path — a Manager
clicks Generate and `app/api/reports.py` runs it under their session.

This module is the *agent* path — the supervisor's specialist for §8's Reporting
row. The division is deliberate and neither half duplicates the other:

* **Facts are never assembled twice.** This agent computes no report section. It
  takes an already-assembled facts object and quotes its `as_payload()`, which is
  the same mapping `drafts.py` freezes into the artifact (R4) and the same one
  `assert_grounded` checks the prose against (R1). One set of figures, one
  grounding set, one frozen payload.

* **Prose goes through the agent runtime, not through `ReportNarrator`.** Not
  because the narrator is wrong, but because `AgentRuntime.generate()` is the
  only method in this layer that returns prose, and it is the only one that joins
  the §11 invocation record to the tool calls the dispatcher accumulated. An
  agent that reached around it would lose the tool half of the log and would call
  the gateway outside the seam that grounds it. The two paths share the check
  that matters — both end in `app.agents.grounding.assert_grounded`, so there is
  exactly one definition of "grounded" in the codebase.

* **The agent adds what the service cannot see.** The service is handed rows; the
  agent *reads* through an R3-gated toolset. `REPORTING_TOOLS` grants
  `read_program`, `list_program_tasks`, `list_program_documents`, `search_corpus`
  and `save_draft`, so it can put the operational picture — open tasks, unsigned
  document obligations — next to the reported figures and flag the gaps a
  reviewer must look at before the report goes anywhere.

NOTHING UNDER `app/agents/` IMPORTS `app/services/`
===================================================
This module types the facts it accepts as a `ReportFacts` protocol rather than
importing `GovernanceReport`, `FeedbackSynthesis` and `CollegeSummary`.
`app.services.reporting.narration` already redeclares its `Completer` "to keep
`app/services/` from depending on `app/agents/`"; this is the same wall from the
other side, and it is load-bearing rather than stylistic —
`app/services/reporting/__init__.py` imports `narration`, which imports
`app.agents.grounding`, so an import in this direction at module scope would make
the cycle real the day somebody adds this agent to `app/agents/__init__.py`.
All three service objects satisfy the protocol exactly as written.

§14 Q3 IS CARRIED, NOT ANSWERED
===============================
    §14 Q3  "Approval authority for college-facing comms: Manager or Senior
             Manager?"

A governance report and a college summary are both college-facing artifacts, and
neither `ArtifactType.GOVERNANCE_REPORT` nor `ArtifactType.PROGRAM_DOCUMENT` has
an entry in `APPROVAL_AUTHORITY`, so `approver_personas()` raises for both. That
is correct and this module does not work around it: §14 says carry the open
question, do not invent an answer.

What it does instead is flag it on every college-facing draft, so the block is
visible when the draft is written rather than discovered at the moment somebody
clicks Approve. `app.services.reporting.drafts.approval_readiness()` reports the
same thing on the service path — reported twice, resolved nowhere, which is the
right ratio for an open question. This agent names no approver persona at all.

R2 HOLDS HERE TOO, AND IT IS EASY TO MISS
=========================================
A governance report is the one artifact that plausibly carries both delivery
facts and commercials: `GovernanceReport.trainer_cost` is a
`TrainerCostSection` of engine-written net pays, present only when the caller
cleared `can_see_commercials()` (§4, R5). This module therefore computes no
total, no average and no arithmetic of any kind — the same refusal
`TrainerCostSection` itself makes — and a test walks this file's AST to prove
there is not one arithmetic operator in it. Counting is `len()`. Every rupee in a
generated report arrived on the facts payload, having been read back off a
`remuneration_sheets` row the engine wrote.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

import structlog
from pydantic import JsonValue

from app.agents.ports import (
    DocumentSnapshot,
    Draft,
    ProgramSnapshot,
    RetrievedPassage,
    TaskSnapshot,
)
from app.agents.runtime import AgentRuntime, DraftOutcome
from app.agents.tools.catalog import AgentName
from app.domain.enums import ArtifactType, AutonomyLevel, Corpus, LLMTask, Persona, TaskStatus

__all__ = [
    "APPROVAL_AUTHORITY_OPEN_QUESTION",
    "OPEN_TASK_STATUSES",
    "OperationalContext",
    "ReportFacts",
    "ReportingAgent",
]

_log = structlog.get_logger(__name__)


#: §14 Q3, carried onto every college-facing draft this agent produces.
#:
#: Worded as a question and not as a recommendation. The moment this string
#: names a persona, the open question has been answered by an agent module
#: instead of by the owner, and `APPROVAL_AUTHORITY` in `app/domain/enums.py`
#: still would not contain the entry — so approval would fail anyway, and the
#: draft would carry a confident, wrong instruction.
APPROVAL_AUTHORITY_OPEN_QUESTION: Final[str] = (
    "approval authority for this college-facing artifact is UNRESOLVED — CLAUDE.md §14 "
    "Q3 asks whether it is a Manager or a Senior Manager, and the question is open. This "
    "draft names no approver; APPROVAL_AUTHORITY has no entry for this artifact type, so "
    "approval will refuse until a human answers Q3."
)

#: A task that still needs doing. BLOCKED counts as open — it is not finished,
#: and a report that omits blocked work reads as though nothing is stuck.
#:
#: The same three members as `app.agents.supervisor.OPEN_STATUSES`, restated here
#: rather than imported: that module pulls in the LangGraph runtime at import
#: time, and a reporting agent should not depend on the orchestrator's graph
#: machinery to know what "open" means. If a fourth status is ever added, both
#: sets need it.
OPEN_TASK_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
)


@runtime_checkable
class ReportFacts(Protocol):
    """Assembled facts whose every figure was read from a system of record.

    Structural rather than nominal — see the module docstring on why nothing here
    imports `app/services/`. `GovernanceReport`, `FeedbackSynthesis` and
    `CollegeSummary` all satisfy it.

    One method, and the narrowness is the guarantee: this agent can serialise the
    facts and can do nothing else with them. It cannot reach into a section, pull
    a `Decimal` out and combine it with another.
    """

    def as_payload(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True, slots=True)
class OperationalContext:
    """What the agent read through its own tools, alongside the reported facts.

    This is the half of a governance report that a facts object assembled from
    delivery tables does not carry: whether the paperwork is actually on file and
    what is still open on the tracker. A report that says delivery went well while
    the work order is unsigned is a report that will be contradicted later.

    Counts and titles only. No figure here is money and none is computed — the
    counts are `len()` over rows the ports returned.
    """

    college_name: str | None
    stage: str | None
    open_task_count: int
    open_task_titles: tuple[str, ...]
    unsigned_documents: tuple[str, ...]
    document_count: int

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "college_name": self.college_name,
            "stage": self.stage,
            "open_task_count": self.open_task_count,
            "open_task_titles": list(self.open_task_titles),
            "unsigned_documents": list(self.unsigned_documents),
            "document_count": self.document_count,
        }


# --- prompts ------------------------------------------------------------------
#
# Rule 1 is the same sentence in all three, because it is the one that matters
# and a model reads the first rule most carefully. The prompt is courtesy; the
# enforcement is `assert_grounded` inside `AgentRuntime.generate()`.

_FIGURE_RULES: Final[str] = (
    "1. Use ONLY the figures, dates, names and counts in the structured data given to "
    "you. State no number that is not there. Do not add, total, average, estimate, "
    "prorate or round anything.\n"
    "2. A figure you cannot find in the data does not go in the text. Say the data does "
    "not record it.\n"
    "3. Do not describe anything as approved, sent, shared or agreed. A human reviews, "
    "edits and sends this.\n"
)

_GOVERNANCE_SYSTEM: Final[str] = (
    "You write the narrative of a governance report for byteXL's operations team, "
    "covering one college training programme over one reporting period. It is reviewed "
    "internally and may then be shared with the college.\n"
    "\n"
    f"Rules you must follow exactly:\n{_FIGURE_RULES}"
    "4. Structure: delivery, attendance, assessments, feedback, then open risks. Lead "
    "with what happened, not with how it is going.\n"
    "5. The `operations` block is what the tracker holds: unsigned document obligations "
    "and open tasks. Name them as operational gaps — an unsigned work order blocks the "
    "payout cycle.\n"
    "6. Where trainer cost appears, quote each figure exactly and state no total. No "
    "total has been computed and you must not compute one.\n"
    "7. Policy passages are supplied for wording only. Never take a figure from one.\n"
    "8. Neutral and factual. No congratulation, no marketing language."
)

_FEEDBACK_SYSTEM: Final[str] = (
    "You summarise collected feedback on a byteXL training programme for an operations "
    "manager.\n"
    "\n"
    f"Rules you must follow exactly:\n{_FIGURE_RULES}"
    "4. The average, the range and the response counts are given to you. Explain what "
    "they show; do not recompute them and do not infer a trend from one collection.\n"
    "5. Say plainly how many collections carried no score. An average taken over part of "
    "the data must be presented as such.\n"
    "6. Six sentences at most."
)

_COLLEGE_SUMMARY_SYSTEM: Final[str] = (
    "You write a short status summary of every training programme running at one "
    "college, for a byteXL operations manager.\n"
    "\n"
    f"Rules you must follow exactly:\n{_FIGURE_RULES}"
    "4. One short paragraph per programme, in the order given.\n"
    "5. Lead each with the programme's stage and what is outstanding.\n"
    "6. This summary contains NO commercial information. Do not refer to rates, costs, "
    "payouts, invoices or margins, and do not speculate about them."
)

_POLICY_HEADER: Final[str] = (
    "POLICY CONTEXT (wording only — CLAUDE.md §9: policy and context, never figures. Do "
    "not quote any number that appears below):"
)


# --- the agent ----------------------------------------------------------------


@dataclass
class ReportingAgent:
    """Drafts governance reports, feedback synthesis and college summaries.

    Level 2, Draft (§8). Construction asserts the ceiling twice — once in
    `AgentRuntime.__post_init__` via `require_ceiling`, once here with the
    sentence that says why — so an instance wired above Draft fails to build
    rather than failing at the first artifact somebody tries to share.
    """

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.REPORTING:
            raise ValueError(
                f"ReportingAgent needs the reporting runtime, got '{self.runtime.agent.value}'"
            )
        if self.runtime.autonomy > AutonomyLevel.DRAFT:
            raise ValueError(
                "Reporting's ceiling is Draft (CLAUDE.md §8). Its output is college-facing, "
                "and §8 is unconditional: 'Nothing touching money, contracts, or a college "
                "contact goes past level 3.' A human reviews, approves and shares."
            )

    async def draft_governance_report(
        self,
        program_id: UUID,
        facts: ReportFacts,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Draft the governance narrative. FRONTIER tier (§2).

        `LLMTask.GOVERNANCE_REPORT` is the one task in this module that
        `TASK_TIER` routes to the frontier model, and §2 names it explicitly. The
        reason is not that the report is long: it is read by a college, and a
        wrong emphasis in a document somebody else has already forwarded is
        expensive to retract in a way a mis-worded internal summary is not.

        The operational context is read through this agent's own tools and placed
        beside the reported facts, so the narrative can name an unsigned work
        order as the gap it is. Both halves are in one grounded payload: R1 does
        not distinguish between a figure from a delivery table and a count of
        unsigned documents, and neither does the check.

        Raises `UngroundedFigureError` if the narrative states a figure that was
        not in either half. The draft is refused, not repaired.
        """
        operations = await self._read_operational_context(program_id)
        passages = await self.runtime.dispatcher.search_corpus(
            Corpus.SOP.value, "governance reporting: what a periodic college report must cover"
        )
        payload = facts.as_payload()

        grounded_in: dict[str, JsonValue] = {
            "report": payload,
            "operations": operations.as_payload(),
            # Labels only. Passage TEXT goes in the prompt and stays out of the
            # grounded set, so a figure lifted from an SOP page is ungrounded
            # (§9: RAG supplies policy and context, never structured facts).
            "sources": _source_labels(passages),
        }
        body, invocation = await self.runtime.generate(
            LLMTask.GOVERNANCE_REPORT,
            system=_GOVERNANCE_SYSTEM,
            user=_prompt(
                "GOVERNANCE REPORT FACTS (the only figures you may state)",
                grounded_in,
                passages,
            ),
            structured_input=grounded_in,
            context="reporting.governance_report",
        )

        commercial = _carries_commercials(payload)
        draft = Draft(
            artifact_type=ArtifactType.GOVERNANCE_REPORT,
            title=f"Governance report — {operations.college_name or 'unknown college'}",
            body=body,
            payload={"report": payload, "operations": operations.as_payload()},
            program_id=program_id,
            flags=_governance_flags(operations, commercial=commercial),
            grounded_in=grounded_in,
        )
        _log.info(
            "reporting.governance_drafted",
            program_id=str(program_id),
            is_commercial=commercial,
            unsigned_documents=len(operations.unsigned_documents),
            open_tasks=operations.open_task_count,
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_feedback_synthesis(
        self,
        program_id: UUID,
        facts: ReportFacts,
        *,
        program_name: str,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Draft the feedback narrative. Volume tier — §2 routes summaries there.

        The average, the range and the counts were computed by
        `synthesise_feedback()` in `Decimal` before this agent saw anything; the
        model explains them. That is R2's shape applied to something that is not
        money, and the reason is the same: a feedback average that changes in the
        last digit between two runs of the same report is a support ticket.

        `program_name` travels inside the payload rather than in the system
        prompt so it is part of the grounded input — a programme called "bCAP
        2026" licenses the model to write 2026, and a name in the prompt instead
        would make that a grounding violation.
        """
        grounded_in: dict[str, JsonValue] = {
            "program_name": program_name,
            "feedback": facts.as_payload(),
        }
        body, invocation = await self.runtime.generate(
            LLMTask.SUMMARY,
            system=_FEEDBACK_SYSTEM,
            user=_prompt(
                "FEEDBACK, ALREADY SYNTHESISED (the only figures you may state)",
                grounded_in,
                (),
            ),
            structured_input=grounded_in,
            context="reporting.feedback_synthesis",
        )

        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Feedback synthesis — {program_name}",
            body=body,
            payload={"program_name": program_name, "feedback": facts.as_payload()},
            program_id=program_id,
            flags=_feedback_flags(facts.as_payload()),
            grounded_in=grounded_in,
        )
        _log.info("reporting.feedback_drafted", program_id=str(program_id), program=program_name)
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_college_summary(
        self,
        facts: ReportFacts,
        *,
        college_name: str,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Draft the college summary. Volume tier (§2). Carries no commercials.

        No `program_id`: a college summary spans every programme at one college,
        so the draft is filed against the college and `Draft.program_id` is
        `None`. `CollegeSummary` carries no money at all — not even behind a flag
        — because it is the view an LDE Executive works from daily (§4), and this
        agent adds none.

        It is still college-facing, so it carries the §14 Q3 flag: `drafts.py`
        types the same artifact as `PROGRAM_DOCUMENT` for exactly that reason.
        """
        payload = facts.as_payload()
        grounded_in: dict[str, JsonValue] = {"summary": payload}
        body, invocation = await self.runtime.generate(
            LLMTask.SUMMARY,
            system=_COLLEGE_SUMMARY_SYSTEM,
            user=_prompt("COLLEGE SUMMARY FACTS (the only figures you may state)", grounded_in, ()),
            structured_input=grounded_in,
            context="reporting.college_summary",
        )

        flags: list[str] = [APPROVAL_AUTHORITY_OPEN_QUESTION]
        if _carries_commercials(payload):
            # Should be unreachable: `CollegeSummary` has no commercial section.
            # Flagged rather than trusted, because "this type never carries money"
            # is a property of a module this one deliberately does not import.
            flags.append(
                "commercial data present in a college summary — CLAUDE.md §4 walls "
                "commercials off from the LDE Executive; do not share before checking"
            )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"College summary — {college_name}",
            body=body,
            payload={"summary": payload},
            program_id=None,
            flags=tuple(flags),
            grounded_in=grounded_in,
        )
        _log.info("reporting.college_summary_drafted", college=college_name)
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def _read_operational_context(self, program_id: UUID) -> OperationalContext:
        """Read the tracker's view through this agent's own tools. R1's half.

        Every value here came from a tool call through the R3-gated dispatcher.
        `read_program` returning `None` means the programme is out of this
        session's reach (§4 RLS) and is reported as an absence, not filled in —
        the caller sees `college_name: None` and the draft carries a flag.

        Overdue is deliberately not computed. It needs a business date, and
        `app.agents.ports.TaskSnapshot` explains why a node must never read the
        clock: a graph that does cannot be replayed from a checkpoint and get the
        same answer. Open-versus-done needs no date, so that is what is reported.
        """
        program: ProgramSnapshot | None = await self.runtime.dispatcher.read_program(program_id)
        tasks: Sequence[TaskSnapshot] = await self.runtime.dispatcher.list_program_tasks(program_id)
        documents: Sequence[DocumentSnapshot] = (
            await self.runtime.dispatcher.list_program_documents(program_id)
        )

        open_tasks = tuple(task for task in tasks if task.status in OPEN_TASK_STATUSES)
        return OperationalContext(
            college_name=program.college_name if program is not None else None,
            stage=program.stage.value if program is not None else None,
            open_task_count=len(open_tasks),
            open_task_titles=tuple(task.title for task in open_tasks),
            unsigned_documents=tuple(doc.title for doc in documents if not doc.signed),
            document_count=len(documents),
        )


# --- flags: what the reviewer is being asked to look at -----------------------


def _governance_flags(operations: OperationalContext, *, commercial: bool) -> tuple[str, ...]:
    """Reviewer flags for a governance report. Never auto-resolved.

    The §14 Q3 flag is first and is unconditional. A governance report is the
    college-facing artifact Q3 is about, and a reviewer who reads no further
    should still learn that nobody has decided who signs it off.
    """
    flags: list[str] = [APPROVAL_AUTHORITY_OPEN_QUESTION]
    if commercial:
        flags.append(
            "COMMERCIAL — this report carries trainer cost. CLAUDE.md §4: Senior Manager "
            "and Manager only, and an LDE Executive must not receive it"
        )
    if operations.unsigned_documents:
        unsigned = ", ".join(operations.unsigned_documents)
        flags.append(
            f"{len(operations.unsigned_documents)} unsigned document obligation(s): "
            f"{unsigned} — an unsigned work order blocks the payout cycle (CLAUDE.md §7)"
        )
    if operations.open_task_count:
        flags.append(f"{operations.open_task_count} task(s) still open on the tracker")
    if operations.college_name is None:
        flags.append(
            "programme not readable in this session's scope (CLAUDE.md §4) — drafted "
            "without programme context"
        )
    return tuple(flags)


def _feedback_flags(payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """Flags for a feedback synthesis. Internal, so no §14 Q3 flag.

    The one flag that matters is a partial average. `synthesise_feedback()`
    averages only the collections that carry a score and reports how many those
    were; a reviewer who does not notice the difference will quote "the feedback
    score" in a college meeting. The comparison is `!=` between two counts the
    service computed — no arithmetic, and no judgement about how big the gap is.
    """
    flags: list[str] = []
    collections = payload.get("collections")
    scored = payload.get("scored_collections")
    if isinstance(collections, int) and isinstance(scored, int) and scored != collections:
        flags.append(
            f"the average covers {scored} of {collections} collection(s) — present it as "
            "partial, never as 'the feedback score'"
        )
    if payload.get("average_score") is None:
        flags.append("no collection carried a score — there is no average to report")
    return tuple(flags)


def _carries_commercials(payload: Mapping[str, JsonValue]) -> bool:
    """Whether a facts payload carries trainer cost. §4's commercials wall.

    Checks both the explicit flag and the section's presence.
    `GovernanceReport.as_payload()` sets `is_commercial` and only adds
    `trainer_cost` when the section exists, so either alone would do today —
    both are checked because this agent does not import the type that guarantees
    that, and a wall that depends on one key of somebody else's dict should not
    depend on only one key.
    """
    return bool(payload.get("is_commercial")) or "trainer_cost" in payload


# --- prompt helpers -----------------------------------------------------------


def _source_labels(passages: Sequence[RetrievedPassage]) -> list[JsonValue]:
    """Citation labels for retrieved passages. §9: no citation, no answer.

    Labels, not text: a `RetrievedPassage` cannot exist without a document title
    and a section, so everything here is citable, while the body stays out of the
    grounded set so no figure can be lifted from it.
    """
    return [
        {"document": passage.document_title, "section": passage.section} for passage in passages
    ]


def _prompt(
    header: str, payload: Mapping[str, JsonValue], passages: Sequence[RetrievedPassage]
) -> str:
    """Structured facts as JSON, then policy passages as words.

    Two visibly separated blocks, which §9 requires of a hybrid answer. JSON
    rather than prose for the facts, because the model must be able to tell a
    figure it may quote from a sentence it may paraphrase — and because the same
    serialisation is what `assert_grounded` walks.
    """
    blocks = [header, json.dumps(payload, indent=2, sort_keys=True, default=str)]
    if passages:
        blocks.append(_POLICY_HEADER)
        blocks.extend(
            f"{passage.document_title} — {passage.section}\n{passage.text}" for passage in passages
        )
    return "\n\n".join(blocks)
