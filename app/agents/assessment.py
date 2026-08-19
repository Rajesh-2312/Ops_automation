"""Assessment agent. CLAUDE.md §8: "Assessment request assembly, Tech-team chase,
report package". Ceiling: Draft.

THE THREE JOBS, SPLIT ALONG R1'S LINE
=====================================
§8 names three pieces of work, and two of them are bookkeeping wearing a
judgement costume. The split is the same one `app.agents.sourcing` makes, for the
same reason — R1: the database owns truth, the LLM owns language.

* **Request completeness is a set operation.** `missing_fields()` compares the
  assembled request against the fields the Tech/Assessment team needs and returns
  the ones the record does not state. Asking a model which fields are missing is
  asking it to invent a fact that `is None` already knows, and it would
  occasionally miss one — producing a request that looks complete, is sent, and
  bounces back a week before the assessment.

* **Package completeness is counting.** `package_gaps()` counts which
  assessments carry a report link and which do not. Note the wording, which is
  load-bearing: *no report link recorded* is not *no report exists*. The
  `assessments` table holds a `report_url` that ClickUp owns the work behind, so
  an empty column means nobody pasted a link. Reporting that as "the report was
  not produced" would state a fact about the Tech team that this system never
  read. Same discipline as the Delivery Monitor's "not measured is not zero".

* **The request, the chase and the package note are prose.** Those go to the
  model on the volume tier (§2: drafting, chase, summaries), grounded against the
  structured input, and come back as drafts a human edits and sends.

WHERE THE FACTS COME FROM, GIVEN THERE IS NO ASSESSMENT PORT
============================================================
`ASSESSMENT_TOOLS` binds `read_program`, `list_program_tasks`,
`list_internal_contacts`, `search_corpus` and `save_draft`. There is no
`list_assessments` tool, and this module does not invent one: `app.agents.ports`
is the R3 security boundary and widening it is a deliberate, reviewed edit in
another workstream's file.

So assessment facts arrive the way the Payout agent's figures do — as structured
input the *caller* read from the system of record, passed in as a Pydantic model
at the layer boundary (§11). The agent cannot fetch an assessment, therefore it
cannot assert one it was not handed, therefore R1 holds by construction rather
than by care.

NOBODY IS CONTACTED
===================
"Tech-team chase" is a *draft* chase. This agent holds `save_draft` and no send
tool (R3), so it lands in DRAFT for a human (R4). `_internal_only()` additionally
filters who a chase may even be addressed to: Tech/Assessment is an internal
actor (§4), and a chase drafted at a trainer or a college contact would be an
external communication this agent's ceiling does not contemplate and R3 gives it
nothing to deliver anyway.

The report package is the one artifact here that might one day travel to a
college. It does not travel from this agent. §14 Q3 — "approval authority for
college-facing comms: Manager or Senior Manager?" — is open, so
`APPROVAL_AUTHORITY` deliberately has no entry for `PROGRAM_DOCUMENT` and a
complete package is flagged for a human to route. That question is carried, not
answered.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, JsonValue

from app.agents.ports import (
    ContactSnapshot,
    Draft,
    ProgramSnapshot,
    RetrievedPassage,
    TaskSnapshot,
)
from app.agents.runtime import AgentRuntime, DraftOutcome
from app.agents.tools.catalog import AgentName
from app.domain.enums import (
    INTERNAL_PERSONAS,
    ArtifactType,
    AutonomyLevel,
    Corpus,
    LLMTask,
    Persona,
    ProgramStage,
    TaskStatus,
)

__all__ = [
    "REQUIRED_REQUEST_FIELDS",
    "AssessmentAgent",
    "AssessmentReportItem",
    "AssessmentRequestSpec",
    "PackageGaps",
    "missing_fields",
    "package_gaps",
]

_log = structlog.get_logger(__name__)


_REQUEST_SYSTEM: Final[str] = (
    "You draft assessment requests from byteXL operations to byteXL's internal "
    "Tech/Assessment team.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the counts, dates, durations and names in the structured data given to "
    "you. State no number that is not there.\n"
    "2. Do not compute totals, head-counts, durations or dates, and do not convert one "
    "unit into another.\n"
    "3. Write for a colleague who has to schedule and build the assessment: what is being "
    "assessed, for which batch, when, how many learners, how long, delivered how.\n"
    "4. Anything listed under `not_stated_in_record` must be written as 'to be confirmed'. "
    "Do not fill it in, and do not infer it from what is typical.\n"
    "5. Policy passages are quoted with their citation and are the only place a rule may "
    "come from. Never take a count, a date or a duration from a policy passage.\n"
    "6. A human will read, edit and send this. Do not write as if it has been sent."
)

_CHASE_SYSTEM: Final[str] = (
    "You draft short internal follow-up messages from byteXL operations to the internal "
    "Tech/Assessment team about an assessment request that is still open.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the figures, dates and names in the structured data given to you.\n"
    "2. Colleagues, not customers: direct, no apologising, no marketing tone.\n"
    "3. Say what is outstanding and what it blocks, and name the date it is needed by.\n"
    "4. Six sentences at most.\n"
    "5. A human will read, edit and send this. Do not write as if it has been sent."
)

_PACKAGE_SYSTEM: Final[str] = (
    "You summarise an assessment report package for a byteXL operations manager who is "
    "about to review it.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. The counts are given to you. Explain them; do not count, total or recompute "
    "anything.\n"
    "2. Use ONLY the figures, dates and titles in the structured data. Quote every number "
    "exactly as given.\n"
    "3. An assessment listed under `without_report_link` has no report link recorded in "
    "this system. Say exactly that. Do not say the report is missing, late, incomplete or "
    "was not produced — this system does not know either way.\n"
    "4. Lead with what the reviewer must chase or confirm before the package can go "
    "anywhere.\n"
    "5. Four sentences at most. No greeting, no sign-off."
)


# --- the layer boundary: what the caller read, as Pydantic v2 (§11) ---------


class AssessmentRequestSpec(BaseModel):
    """One assessment request, as the caller assembled it from the records.

    Pydantic v2 rather than a dataclass because this is a layer boundary in §11's
    sense — an API handler or a scheduled task builds it from `assessments`,
    `batches` and `programs` rows and hands it across into the agent layer, and
    §11 forbids a raw dict making that crossing.

    Frozen, and `extra="forbid"`: a field nobody declared cannot be smuggled into
    the prompt, where it would become quotable by the model without ever having
    been reviewed as something the agent may state.

    Every optional field is optional because the record genuinely may not hold
    it. `None` means "the system of record does not state this" and is reported
    as such by `missing_fields()` — it is never defaulted, inferred or filled in
    with what is typical (R1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Which batch, as the tracker labels it. Required: an assessment request
    #: that does not say what it is for cannot be actioned by anybody.
    batch_label: str
    syllabus_scope: str | None = None
    scheduled_on: dt.date | None = None
    student_count: int | None = None
    duration_minutes: int | None = None
    #: "online", "on campus", "proctored lab" — free text as the record holds it.
    delivery_mode: str | None = None
    #: The EdTech platform the assessment runs on, when the record names one.
    #: Left out of `REQUIRED_REQUEST_FIELDS` on purpose: §14 Q6 ("EdTech platform
    #: access: direct DB, API, or neither?") is open, so flagging it missing on
    #: every request would train reviewers to ignore the flags that matter.
    platform: str | None = None
    special_requirements: tuple[str, ...] = ()


class AssessmentReportItem(BaseModel):
    """One assessment in a report package, as the `assessments` row holds it.

    Mirrors `app.db.models.Assessment` minus the ids: title, when it was
    conducted, and the two links. `report_url` is a link somebody pasted, so its
    absence means the link is not recorded — see `package_gaps()` on why that
    distinction is worth the extra words.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    conducted_on: dt.date | None = None
    report_url: str | None = None
    clickup_url: str | None = None


# --- deterministic: completeness and gaps, computed without a model ---------


#: The fields the Tech/Assessment team needs before it can schedule and build an
#: assessment. Absent ones are reported to the reviewer and written as "to be
#: confirmed" in the draft, never guessed.
#:
#: `batch_label` is not here because `AssessmentRequestSpec` requires it, and
#: `platform` is not here for the reason its field comment gives.
REQUIRED_REQUEST_FIELDS: Final[tuple[str, ...]] = (
    "syllabus_scope",
    "scheduled_on",
    "student_count",
    "duration_minutes",
    "delivery_mode",
)


def missing_fields(spec: AssessmentRequestSpec) -> tuple[str, ...]:
    """Which required fields the record does not state. Pure; a set operation.

    Returned in `REQUIRED_REQUEST_FIELDS` order rather than in whatever order a
    comprehension happened to visit, so two runs over the same spec produce the
    same list and a reviewer reading two drafts sees the same shape twice.

    A whitespace-only string counts as missing. A syllabus scope of `" "` is a
    field somebody tabbed past, and treating it as stated would put an empty
    scope in front of the Tech team as though it had been decided.
    """
    stated: Mapping[str, object] = {
        "syllabus_scope": spec.syllabus_scope,
        "scheduled_on": spec.scheduled_on,
        "student_count": spec.student_count,
        "duration_minutes": spec.duration_minutes,
        "delivery_mode": spec.delivery_mode,
    }
    return tuple(
        name
        for name in REQUIRED_REQUEST_FIELDS
        if _is_blank(stated[name])  # KeyError here means the two lists drifted apart
    )


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


@dataclass(frozen=True, slots=True)
class PackageGaps:
    """What a report package is and is not carrying. Counted, never estimated.

    `without_report_link` is deliberately not called `missing_reports`. The
    `assessments` row holds a `report_url` that a human pastes once ClickUp has
    the artefact; an empty column means **this system has no link on file**, which
    is a different claim from "the Tech team did not produce a report". The agent
    may state the first and must never state the second, and the prompt says so
    in those words.
    """

    total: int
    with_report_link: int
    without_report_link: tuple[str, ...]
    undated: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """True when every assessment in a non-empty package has a link on file."""
        return self.total > 0 and not self.without_report_link

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "total": self.total,
            "with_report_link": self.with_report_link,
            "without_report_link_count": len(self.without_report_link),
            "without_report_link": list(self.without_report_link),
            "undated": list(self.undated),
        }


def package_gaps(items: Sequence[AssessmentReportItem]) -> PackageGaps:
    """Count what the package holds. Pure, deterministic, no LLM.

    Titles are carried rather than counts alone: "3 assessments have no report
    link" sends a reviewer hunting through the batch, and naming them is the work
    this function exists to remove.
    """
    without = tuple(item.title for item in items if not (item.report_url or "").strip())
    undated = tuple(item.title for item in items if item.conducted_on is None)
    return PackageGaps(
        total=len(items),
        with_report_link=len(items) - len(without),
        without_report_link=without,
        undated=undated,
    )


def _internal_only(contacts: Sequence[ContactSnapshot]) -> tuple[ContactSnapshot, ...]:
    """Drop external parties. §4's internal personas; §8's ceiling on this agent.

    Tech/Assessment is an internal actor. A chase addressed at a trainer or a
    college contact would be an external communication, which no draft agent in
    Phase 4 contemplates and which R3 gives nothing the capability to deliver.
    """
    return tuple(contact for contact in contacts if contact.persona in INTERNAL_PERSONAS)


def _open_assessment_tasks(tasks: Sequence[TaskSnapshot]) -> tuple[TaskSnapshot, ...]:
    """Open tasks in the monitoring stage, where assessments live.

    Narrowed to one stage because a program carries dozens of task rows and a
    chase prompt full of closed acquisition tasks is noise. Closed tasks are
    dropped for the same reason: a chase is about what is still open.
    """
    return tuple(
        task
        for task in tasks
        if task.stage is ProgramStage.ACTIVE_MONITORING and task.status is not TaskStatus.DONE
    )


# --- the agent ---------------------------------------------------------------


@dataclass
class AssessmentAgent:
    """Drafts assessment requests, Tech-team chases and package notes. Level 2.

    Construction asserts §8's ceiling twice over: `AgentRuntime.__post_init__`
    calls `require_ceiling`, and this class refuses anything above Draft with a
    message that names the rule. An agent wired too high fails to build rather
    than failing at the first release attempt.
    """

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.ASSESSMENT:
            raise ValueError(
                f"AssessmentAgent needs the assessment runtime, got "
                f"'{self.runtime.agent.value}'"
            )
        if self.runtime.autonomy > AutonomyLevel.DRAFT:
            raise ValueError(
                "The Assessment agent's ceiling is Draft (CLAUDE.md §8). It proposes a "
                "request, a chase and a package note; a human edits and sends them."
            )

    async def draft_assessment_request(
        self,
        program_id: UUID,
        spec: AssessmentRequestSpec,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Assemble the assessment request for the Tech team. §8's first job.

        Completeness is decided by `missing_fields()` before the model is called
        and passed in as structured input, so the draft says "to be confirmed"
        about a field that was calculated to be absent rather than one the model
        happened to notice. Every gap also becomes a `Draft` flag, which is what a
        reviewer sees first.

        The SOP corpus is searched for the request procedure. §9: policy and
        context only — the citation and the passage go into the prompt, and the
        prompt forbids taking any count, date or duration from one.
        """
        program = await self.runtime.dispatcher.read_program(program_id)
        procedure = await self.runtime.dispatcher.search_corpus(
            Corpus.SOP.value,
            "assessment request procedure, notice period, question paper format",
        )
        absent = missing_fields(spec)

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            "request": spec.model_dump(mode="json"),
            "not_stated_in_record": list(absent),
            "procedure": _cite(procedure),
        }
        body, invocation = await self.runtime.generate(
            LLMTask.DRAFTING,
            system=_REQUEST_SYSTEM,
            user=(
                "ASSESSMENT REQUEST (the only figures, dates and names you may state):\n"
                f"{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="assessment.request",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Assessment request — {_college_of(program)}, {spec.batch_label}",
            body=body,
            payload={"request": spec.model_dump(mode="json"), "not_stated": list(absent)},
            program_id=program_id,
            flags=tuple(f"not stated in the record: {name}" for name in absent),
            grounded_in=grounded_in,
        )
        _log.info(
            "assessment.request_drafted",
            program_id=str(program_id),
            batch=spec.batch_label,
            fields_missing=len(absent),
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_tech_team_chase(
        self,
        program_id: UUID,
        spec: AssessmentRequestSpec,
        *,
        requested_on: dt.date,
        as_of: dt.date,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Draft an internal chase about an open assessment request. §8's second job.

        `as_of` is passed in and never read from a clock. A graph that reads the
        clock inside a node cannot be replayed from its Postgres checkpoint (§8)
        and get the same answer, which is the same rule
        `app.agents.ports.TaskSnapshot` states about overdue dates.

        `days_outstanding` is subtracted in Python and handed to the model, which
        is R2's shape applied to something that is not money: an agent may explain
        a number, it may never produce one. Recipients are filtered to internal
        personas before the model is called, so an external contact is not even in
        the prompt.
        """
        if as_of < requested_on:
            raise ValueError(
                f"as_of {as_of} precedes requested_on {requested_on} — a request cannot "
                "have been outstanding for a negative number of days, and reporting one "
                "would put a nonsense figure in front of the Tech team."
            )
        days_outstanding = (as_of - requested_on).days

        program = await self.runtime.dispatcher.read_program(program_id)
        tasks = await self.runtime.dispatcher.list_program_tasks(program_id)
        contacts = _internal_only(await self.runtime.dispatcher.list_internal_contacts(program_id))

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            "request": spec.model_dump(mode="json"),
            "requested_on": requested_on.isoformat(),
            "as_of": as_of.isoformat(),
            "days_outstanding": days_outstanding,
            "open_assessment_tasks": [
                {
                    "title": task.title,
                    "status": task.status.value,
                    "due_on": task.due_on.isoformat() if task.due_on else None,
                }
                for task in _open_assessment_tasks(tasks)
            ],
            "recipients": [contact.display_name for contact in contacts],
        }
        body, invocation = await self.runtime.generate(
            LLMTask.CHASE,
            system=_CHASE_SYSTEM,
            user=(
                "OPEN ASSESSMENT REQUEST (the only figures, dates and names you may "
                f"state):\n{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="assessment.chase",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Tech-team follow-up — {_college_of(program)}, {spec.batch_label}",
            body=body,
            payload={
                "recipients": [contact.display_name for contact in contacts],
                "days_outstanding": days_outstanding,
            },
            program_id=program_id,
            flags=() if contacts else ("no internal recipient identified — assign before sending",),
            grounded_in=grounded_in,
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_report_package(
        self,
        program_id: UUID,
        items: Sequence[AssessmentReportItem],
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Summarise the assessment report package for review. §8's third job.

        Every count comes from `package_gaps()`, computed before the model sees
        anything; the model writes the covering note. An assessment with no
        `report_url` is reported as having no link recorded — never as a report
        that is missing or late, because this system did not read that.

        A package with a link for every assessment is flagged rather than
        forwarded: §14 Q3 (approval authority for college-facing comms) is open,
        `APPROVAL_AUTHORITY` has no entry for this artifact type, and picking the
        permissive answer to make the flow complete would be inventing governance.
        """
        program = await self.runtime.dispatcher.read_program(program_id)
        gaps = package_gaps(items)

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            "package": gaps.as_payload(),
            "assessments": [
                {
                    "title": item.title,
                    "conducted_on": item.conducted_on.isoformat() if item.conducted_on else None,
                    "report_link_recorded": bool((item.report_url or "").strip()),
                }
                for item in items
            ],
        }
        body, invocation = await self.runtime.generate(
            LLMTask.SUMMARY,
            system=_PACKAGE_SYSTEM,
            user=(
                "ASSESSMENT REPORT PACKAGE, already counted. Explain it; count nothing:\n"
                f"{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="assessment.package",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Assessment report package — {_college_of(program)}",
            body=body,
            payload={"package": gaps.as_payload()},
            program_id=program_id,
            flags=_package_flags(gaps),
            grounded_in=grounded_in,
        )
        _log.info(
            "assessment.package_drafted",
            program_id=str(program_id),
            assessments=gaps.total,
            without_report_link=len(gaps.without_report_link),
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )


def _package_flags(gaps: PackageGaps) -> tuple[str, ...]:
    """What the reviewer is being asked to look at. Never auto-resolved.

    The empty-package case is its own flag rather than folded into the incomplete
    one: a package of nothing and a package with gaps need different people
    chased, and a summary that says "0 of 0 assessments have links" reads as
    healthy to a reader skimming.
    """
    if gaps.total == 0:
        return ("no assessments supplied for this package",)
    flags: list[str] = []
    if gaps.without_report_link:
        flags.append(
            f"{len(gaps.without_report_link)} assessment(s) have no report link recorded — "
            "confirm with the Tech team before the package is released"
        )
    if gaps.undated:
        flags.append(f"{len(gaps.undated)} assessment(s) have no conducted date recorded")
    if gaps.is_complete:
        flags.append(
            "package is complete — release authority for a college-facing artifact is "
            "unresolved (CLAUDE.md §14 Q3), so route it for approval manually"
        )
    return tuple(flags)


def _cite(passages: Sequence[RetrievedPassage]) -> list[JsonValue]:
    """Retrieved passages with their citations. §9: no citation, no answer."""
    return [
        {"source": f"{passage.document_title} § {passage.section}", "text": passage.text}
        for passage in passages
    ]


def _program_payload(program: ProgramSnapshot | None) -> JsonValue:
    """The program snapshot as JSON, or `None` when it was out of reach (§4 RLS)."""
    if program is None:
        return None
    return {
        "college_name": program.college_name,
        "program_type": program.program_type,
        "stage": program.stage.value,
        "starts_on": program.starts_on.isoformat() if program.starts_on else None,
        "ends_on": program.ends_on.isoformat() if program.ends_on else None,
    }


def _college_of(program: ProgramSnapshot | None) -> str:
    return program.college_name if program is not None else "unknown program"
