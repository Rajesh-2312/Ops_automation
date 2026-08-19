"""Onboarding agent. CLAUDE.md §8: "WO / ZOHO / ERM / platform-access checklist,
internal chase". Ceiling: Auto (internal only).

WHAT "AUTO (INTERNAL ONLY)" DOES AND DOES NOT BUY
=================================================
`app.agents.runtime.AGENT_CEILINGS` puts this agent at `AutonomyLevel.ACT` — the
only specialist above Draft. That is the one thing about this module a reader
must get right, so it is stated first and stated plainly:

**A high ceiling grants no send capability.** R3 is unconditional. It does not
say "except for internal messages" and it does not bend for a level-4 agent. The
toolset in `app.agents.tools.catalog.ONBOARDING_TOOLS` is four reads and
`save_draft`, and `__post_init__` below re-asserts that the toolset's *effects*
are drawn from the two R3 permits — a check that is redundant today and is here
for the day somebody reasons "it is level 4, so it may send".

What level 4 actually describes is two things, neither of which is a tool:

1. **The checklist is produced without a human in the loop.** `assess_onboarding`
   calls no model, asks nobody, and returns a fact about the tracker. Running it
   hourly and acting on the result — opening a task, marking a step outstanding —
   is internal state moving inside byteXL, which is what "Auto (internal only)"
   permits. R1 is the reason it needs no model: what is signed, what is done and
   what is blocked are database facts, and a model asked to judge them would be
   inventing a fact it could have read.

2. **A chase this agent drafts *may* be released by the Comms Service under a
   human-set policy**, without a per-message click. That release is performed by
   a service, on a rule a human wrote, against a queue whose recipient class is
   `internal_staff`. It is not performed by this agent, which has no mechanism to
   perform it. CLAUDE.md §8 conditions even that on "a demonstrated track
   record", and §14 leaves the exact policy unspecified — so no policy is
   invented here. This module drafts; what the queue does next is the queue's.

NOBODY EXTERNAL IS ADDRESSED, LET ALONE CONTACTED
=================================================
`_internal_only()` filters recipients to `INTERNAL_PERSONAS` (§4) *before* the
model is called, so a trainer or a college contact is not in the prompt at all.
Onboarding is internal work by definition — TA, HR, Finance & Accounts, Tech,
Platform, Central OPS are the actors in §4's list — and a chase addressed outside
that set would be an external communication from the one agent whose ceiling
could theoretically auto-release it. That combination is the specific hazard this
filter exists for. Filtering in the consumer rather than trusting the port is the
same move `app.agents.supervisor.internal_recipients()` makes, for the same
reason: a future port implementation that forgets the filter must not be able to
make this agent address a college.

R1: THE CHECKLIST IS COMPUTED, THE COVERING NOTE IS WRITTEN
===========================================================
`build_checklist()` is pure Python over `TaskSnapshot`s and `DocumentSnapshot`s.
Nothing about a step's state is a model's opinion. The model receives the
finished checklist and writes the chase, and `AgentRuntime.generate` refuses the
text if it states a figure the checklist did not contain.

THE STEP NOBODY CREATED A TASK FOR
==================================
`OnboardingChecklist` is **total over `OnboardingStep`**: all four of §8's items
appear on every checklist, and a step with no task and no document behind it
comes back `NOT_TRACKED` rather than being absent. That is the finding this agent
is worth having for. A checklist that lists three of four steps and omits the one
nobody opened a task for reads as complete, which is exactly how a trainer
reaches a campus without platform access.

ERM STAYS MANUAL (§10)
======================
This agent reports the state of the ERM step. It does not sync it, and there is
no ERM port in `app.agents.ports` for it to sync through. §10 models ERM as a
sync task with a generated field pack that a named human pastes — that lives in
`app/services/erm/`, which owns the drift detection and the `erm_stale` flip.
Nothing here scrapes, and nothing here claims a record was synced.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

import structlog
from pydantic import JsonValue

from app.agents.ports import ContactSnapshot, DocumentSnapshot, Draft, ProgramSnapshot, TaskSnapshot
from app.agents.runtime import AgentRuntime, DraftOutcome
from app.agents.tools.catalog import AgentName, ToolEffect
from app.domain.enums import (
    INTERNAL_PERSONAS,
    ArtifactType,
    LLMTask,
    Persona,
    ProgramStage,
    TaskStatus,
)

__all__ = [
    "ChecklistItem",
    "OnboardingAgent",
    "OnboardingChecklist",
    "OnboardingStep",
    "StepState",
    "build_checklist",
]

_log = structlog.get_logger(__name__)


class OnboardingStep(StrEnum):
    """§8's four onboarding items, as a closed vocabulary.

    WHY THIS LIVES HERE AND NOT IN `app/domain/enums.py`
    ----------------------------------------------------
    §11 says "Enums in `domain/`, never string literals for status values", and
    the general rule is right. This one follows the precedent already set three
    times in this codebase — `app.core.audit.AuditAction`,
    `app.services.approval.state_machine.ApprovalAction` and
    `app.agents.tools.catalog.ToolEffect` — each of which documents the same
    carve-out: a vocabulary owned by exactly one module, not stored in a column
    and not mirrored by a Postgres enum, belongs beside its only consumer.
    `app/services/comms/types.py` states the condition for moving it, and the
    same condition applies here: if a column ever stores one of these labels,
    move the class to `domain/` verbatim and re-export from here for one release.

    The membership is transcribed from §8's "Owns" column — WO, ZOHO, ERM,
    platform access — and adding a fifth step is a decision about what onboarding
    *is*, not a refactor.
    """

    WORK_ORDER = "work_order"
    ZOHO = "zoho"
    ERM = "erm"
    PLATFORM_ACCESS = "platform_access"


class StepState(StrEnum):
    """Where one onboarding step stands. Derived, never stored, never guessed.

    `NOT_TRACKED` is the member that earns this enum. Without it a step with no
    task and no document behind it would have to be reported as either done
    (false, and dangerous) or outstanding (misleading — nobody is working on it
    because nobody created it). Naming the third case is what lets the chase say
    "open a task for this" rather than "hurry up with this".

    `BLOCKED` wins over `OUTSTANDING` when both apply, because unblocking is the
    actionable half — the same precedence `app.agents.supervisor.assess()` uses.
    """

    DONE = "done"
    OUTSTANDING = "outstanding"
    BLOCKED = "blocked"
    NOT_TRACKED = "not_tracked"


#: Title phrases that identify a step. Lower-cased, whitespace-collapsed, matched
#: on **word boundaries** rather than as bare substrings.
#:
#: The word boundary is not fussiness. `"erm"` as a substring matches "terms",
#: "determine" and "permission", so a task titled "Confirm the terms with the
#: college" would silently become evidence that ERM onboarding is under way. A
#: checklist that reports a step as in-progress because of an unrelated task is
#: worse than one that reports it untracked, because only the second gets fixed.
#:
#: A task or document may legitimately match more than one step ("Push the signed
#: work order into ERM" matches two) and is then counted as evidence for both.
#: Dropping it from one would hide an obligation, which is the failure mode this
#: whole module exists to prevent; showing one task twice is merely untidy.
#:
#: Nothing here is fuzzy. `"wo"` is absent on purpose. A step whose task the
#: tracker titles in words this table does not know comes back `NOT_TRACKED` —
#: a visible, fixable state. A fuzzy match that silently attaches the wrong task
#: is not.
_STEP_KEYWORDS: Final[Mapping[OnboardingStep, tuple[str, ...]]] = {
    OnboardingStep.WORK_ORDER: ("work order", "work-order", "workorder"),
    OnboardingStep.ZOHO: ("zoho",),
    OnboardingStep.ERM: ("erm",),
    OnboardingStep.PLATFORM_ACCESS: (
        "platform access",
        "platform login",
        "platform credential",
        "platform credentials",
        "lms access",
    ),
}

_STEP_PATTERNS: Final[Mapping[OnboardingStep, re.Pattern[str]]] = MappingProxyType(
    {
        step: re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b")
        for step, words in _STEP_KEYWORDS.items()
    }
)

#: Recorded on every chase draft this agent saves, so the audience is a property
#: of the artifact rather than something inferred from who happens to be in
#: `recipients`. §8 permits a downstream queue to release an *internal* chase
#: under a human-set policy; a queue that has to guess the audience will
#: eventually guess wrong, and this agent is the one where guessing wrong means
#: an unreviewed message reaching a college.
#:
#: The value matches `CommsRecipientKind.INTERNAL_STAFF` in
#: `app/services/comms/types.py`, which is deliberately **not** imported: nothing
#: under `app/agents/` should take a dependency on the outbound queue, and an
#: agent module importing anything named `comms` is the shape R3 exists to keep
#: out of this package even when the import is only two enums.
_INTERNAL_AUDIENCE: Final[str] = "internal_staff"

_CHASE_SYSTEM: Final[str] = (
    "You draft short internal chase messages from byteXL operations to colleagues who own "
    "outstanding trainer-onboarding steps.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the figures, dates, names and step states in the structured data given to "
    "you. State no number that is not there.\n"
    "2. Do not count, total or compute anything. The counts you need are given.\n"
    "3. Colleagues, not customers: direct, no apologising, no marketing tone.\n"
    "4. A step marked not_tracked has no task behind it — ask for the task to be opened, "
    "do not ask for it to be hurried.\n"
    "5. A step marked blocked needs unblocking, not chasing.\n"
    "6. Six sentences at most.\n"
    "7. A human will read, edit and send this. Do not write as if it has been sent."
)


# --- deterministic: the checklist -------------------------------------------


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One of §8's four steps, with the tracker rows behind it.

    `tasks` and `documents` are the evidence, carried rather than summarised, so
    a reviewer can see *why* a step is outstanding without a second query. That
    is R1 in a small way: the state is not an assertion, it is a derivation from
    rows, and the rows travel with it.
    """

    step: OnboardingStep
    state: StepState
    tasks: tuple[TaskSnapshot, ...]
    documents: tuple[DocumentSnapshot, ...]
    #: Internal owners of the unfinished tasks on this step. Externally-owned
    #: tasks contribute no owner here — see `_internal_only`.
    owners: tuple[ContactSnapshot, ...]

    def as_payload(self) -> dict[str, JsonValue]:
        """The item as structured input for the chase prompt."""
        return {
            "step": self.step.value,
            "state": self.state.value,
            "tasks": [
                {
                    "title": task.title,
                    "status": task.status.value,
                    "due_on": task.due_on.isoformat() if task.due_on else None,
                }
                for task in self.tasks
            ],
            "documents": [
                {"title": document.title, "status": document.status, "signed": document.signed}
                for document in self.documents
            ],
            "owners": [owner.display_name for owner in self.owners],
        }


@dataclass(frozen=True, slots=True)
class OnboardingChecklist:
    """All four steps, always. See the module docstring on `NOT_TRACKED`.

    `items` is total over `OnboardingStep` and in the enum's declaration order,
    so a rendered checklist has the same rows in the same places on every
    program. An operator learns where to look once.
    """

    program_id: UUID
    items: tuple[ChecklistItem, ...]
    #: Tasks the tracker filed under TRAINER_ONBOARDING that match none of the
    #: four steps. Reported, not force-fitted: §8 names four items and a fifth
    #: kind of onboarding work is a human's to classify.
    unclassified_tasks: tuple[TaskSnapshot, ...]

    def of(self, *states: StepState) -> tuple[ChecklistItem, ...]:
        return tuple(item for item in self.items if item.state in states)

    @property
    def outstanding(self) -> tuple[ChecklistItem, ...]:
        return self.of(StepState.OUTSTANDING)

    @property
    def blocked(self) -> tuple[ChecklistItem, ...]:
        return self.of(StepState.BLOCKED)

    @property
    def not_tracked(self) -> tuple[ChecklistItem, ...]:
        return self.of(StepState.NOT_TRACKED)

    @property
    def is_complete(self) -> bool:
        """True only when every step is DONE. `NOT_TRACKED` is never complete."""
        return all(item.state is StepState.DONE for item in self.items)

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "steps": [item.as_payload() for item in self.items],
            "outstanding_count": len(self.outstanding),
            "blocked_count": len(self.blocked),
            "not_tracked_count": len(self.not_tracked),
            "unclassified_onboarding_tasks": [task.title for task in self.unclassified_tasks],
        }


def _normalise(title: str) -> str:
    return " ".join(title.lower().split())


def _matches(title: str, step: OnboardingStep) -> bool:
    return _STEP_PATTERNS[step].search(_normalise(title)) is not None


def _internal_only(contacts: Sequence[ContactSnapshot]) -> tuple[ContactSnapshot, ...]:
    """Drop external parties. §4's internal personas; §8's "internal only".

    Applied to chase recipients and to task owners alike. A trainer is a record
    and not a user (§4) — an onboarding chase addressed at one would be an
    external message from the one agent whose ceiling permits auto-release.
    """
    return tuple(contact for contact in contacts if contact.persona in INTERNAL_PERSONAS)


def _state_of(
    tasks: Sequence[TaskSnapshot],
    documents: Sequence[DocumentSnapshot],
    done_ids: frozenset[UUID],
) -> StepState:
    """Derive one step's state from its evidence. Pure, and no model involved.

    "Blocked" is two things at once, both of which matter, exactly as
    `app.agents.supervisor.assess()` treats them: a task the tracker marked
    BLOCKED, and an unfinished task whose `blocked_by` predecessors are not all
    DONE. The second catches what the tracker has not caught up with, and the
    dependency graph is the truth of which the status column is a cache.
    """
    if not tasks and not documents:
        return StepState.NOT_TRACKED
    if any(
        task.status is TaskStatus.BLOCKED
        or (
            task.status is not TaskStatus.DONE
            and any(dependency not in done_ids for dependency in task.blocked_by)
        )
        for task in tasks
    ):
        return StepState.BLOCKED
    tasks_done = all(task.status is TaskStatus.DONE for task in tasks)
    documents_signed = all(document.signed for document in documents)
    return StepState.DONE if tasks_done and documents_signed else StepState.OUTSTANDING


def build_checklist(
    program_id: UUID,
    documents: Sequence[DocumentSnapshot],
    tasks: Sequence[TaskSnapshot],
) -> OnboardingChecklist:
    """§8's four-item checklist, computed from tracker rows. Pure — no I/O, no LLM.

    Steps are matched across *every* task and document on the program rather than
    only those filed under `TRAINER_ONBOARDING`: a work order obligation is a work
    order obligation whatever stage the tracker put it in, and a checklist that
    missed one because of a stage label would be wrong in the direction that
    hurts.

    The `unclassified_tasks` bucket is stage-scoped the other way — only tasks the
    tracker itself called onboarding. A program-wide "everything that matched
    nothing" list would be every task on the program, which is noise, and a noisy
    checklist is one nobody reads.
    """
    done_ids = frozenset(task.task_id for task in tasks if task.status is TaskStatus.DONE)
    items: list[ChecklistItem] = []
    classified: set[UUID] = set()

    for step in OnboardingStep:
        step_tasks = tuple(task for task in tasks if _matches(task.title, step))
        step_documents = tuple(document for document in documents if _matches(document.title, step))
        classified.update(task.task_id for task in step_tasks)
        owners = _internal_only(
            [
                task.owner
                for task in step_tasks
                if task.owner is not None and task.status is not TaskStatus.DONE
            ]
        )
        items.append(
            ChecklistItem(
                step=step,
                state=_state_of(step_tasks, step_documents, done_ids),
                tasks=step_tasks,
                documents=step_documents,
                owners=owners,
            )
        )

    unclassified = tuple(
        task
        for task in tasks
        if task.stage is ProgramStage.TRAINER_ONBOARDING and task.task_id not in classified
    )
    return OnboardingChecklist(
        program_id=program_id, items=tuple(items), unclassified_tasks=unclassified
    )


# --- the agent ---------------------------------------------------------------


@dataclass
class OnboardingAgent:
    """Checklist and internal chase. §8: "Auto (internal only)" — see the module
    docstring for what that does and does not permit."""

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.ONBOARDING:
            raise ValueError(
                f"OnboardingAgent needs the onboarding runtime, got "
                f"'{self.runtime.agent.value}'"
            )
        # R3, re-asserted at the one agent where a reader might think the rule
        # bends. It does not: "Auto (internal only)" is a statement about what may
        # happen to a draft downstream, not a capability grant. This check is
        # redundant against `AgentToolset.of()` today and is here so that the day
        # somebody widens the toolset "temporarily", the agent refuses to build.
        permitted = {ToolEffect.READ, ToolEffect.SAVE_DRAFT}
        held = self.runtime.dispatcher.toolset.effects
        if not held <= permitted:
            raise PermissionError(
                f"Onboarding's toolset holds {sorted(str(effect) for effect in held - permitted)} "
                "beyond read and save_draft. CLAUDE.md R3 is unconditional and does not bend "
                "for a level-4 ceiling: an agent has no release capability regardless of its "
                "autonomy level."
            )

    async def assess_onboarding(self, program_id: UUID) -> OnboardingChecklist:
        """The §8 checklist, read from the tracker. No model is called.

        This is the level-4 half of the agent: it runs unattended, it asserts
        nothing it did not read, and it contacts nobody. R1 is why there is no
        LLM here — whether a work order is signed is a database fact, and a model
        asked to judge it would be inventing what it could have read.
        """
        documents = await self.runtime.dispatcher.list_program_documents(program_id)
        tasks = await self.runtime.dispatcher.list_program_tasks(program_id)
        checklist = build_checklist(program_id, documents, tasks)
        _log.info(
            "onboarding.assessed",
            program_id=str(program_id),
            outstanding=len(checklist.outstanding),
            blocked=len(checklist.blocked),
            not_tracked=len(checklist.not_tracked),
            complete=checklist.is_complete,
        )
        return checklist

    async def draft_internal_chase(
        self,
        program_id: UUID,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Draft a chase to the internal owners of the outstanding steps.

        The checklist is computed before the model is called and passed in as
        structured input, so the prose describes a state that was derived rather
        than one that was noticed — and `AgentRuntime.generate` refuses the text
        if it states a count the checklist did not contain.

        Recipients are filtered to internal personas first, so an external contact
        is never in the prompt. Combined with R3 — this agent has no send tool —
        the chase is a draft addressed to a colleague, and that is as far as any
        agent in this layer goes regardless of ceiling.
        """
        program = await self.runtime.dispatcher.read_program(program_id)
        documents = await self.runtime.dispatcher.list_program_documents(program_id)
        tasks = await self.runtime.dispatcher.list_program_tasks(program_id)
        contacts = _internal_only(await self.runtime.dispatcher.list_internal_contacts(program_id))
        checklist = build_checklist(program_id, documents, tasks)

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            "checklist": checklist.as_payload(),
            "recipients": [contact.display_name for contact in contacts],
        }
        body, invocation = await self.runtime.generate(
            LLMTask.CHASE,
            system=_CHASE_SYSTEM,
            user=(
                "ONBOARDING CHECKLIST (the only figures, dates, names and states you may "
                f"state):\n{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="onboarding.internal_chase",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Onboarding chase — {_college_of(program)}",
            body=body,
            payload={
                "checklist": checklist.as_payload(),
                "recipients": [contact.display_name for contact in contacts],
                "audience": _INTERNAL_AUDIENCE,
            },
            program_id=program_id,
            flags=_chase_flags(checklist, contacts),
            grounded_in=grounded_in,
        )
        _log.info(
            "onboarding.chase_drafted",
            program_id=str(program_id),
            recipients=len(contacts),
            outstanding=len(checklist.outstanding),
            blocked=len(checklist.blocked),
            not_tracked=len(checklist.not_tracked),
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )


def _chase_flags(
    checklist: OnboardingChecklist, contacts: Sequence[ContactSnapshot]
) -> tuple[str, ...]:
    """Non-blocking observations for the reviewer. Never auto-resolved.

    The `NOT_TRACKED` flags come first because they are the ones that change what
    a human does: every other flag asks somebody to finish work, and these ask
    somebody to create it.
    """
    flags: list[str] = [
        f"no task or document tracks the {item.step.value} step — open one"
        for item in checklist.not_tracked
    ]
    if not contacts:
        flags.append("no internal recipient identified — assign before sending")
    if checklist.is_complete:
        flags.append("every onboarding step is complete — no chase is needed")
    if checklist.unclassified_tasks:
        flags.append(
            f"{len(checklist.unclassified_tasks)} onboarding task(s) match no checklist step"
        )
    return tuple(flags)


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
