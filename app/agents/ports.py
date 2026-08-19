"""The narrow surface agents are allowed to touch. CLAUDE.md R1 and R3.

    R1  "The database owns truth. The LLM owns language."
    R3  "Agent tool sets contain read and `save_draft` only."

WHY THIS FILE IS A PROTOCOL AND NOT A REPOSITORY
================================================
Phase 4 owns the agent layer; it does not own the schema. Binding agents to real
tables is a later, separate change by whoever owns `app/db/models.py`. So the
contract is stated here as `typing.Protocol` and the tests supply a fake. Nothing
in `app/agents/` imports `app.db`, which also means an agent cannot open a
session and issue a statement of its own devising.

THE PROTOCOL SURFACE *IS* THE SECURITY BOUNDARY
===============================================
This is the second of the two locks that make R3 structural (the first is in
`app.agents.tools.catalog`: a toolset is pure data and cannot hold a callable).
Every tool call an agent makes is dispatched to a method on one of these
protocols. Look at what is here: reads, and one `save_draft`. There is no
`send_email`, no `post_message`, no `mark_released`, and no generic `execute`.

That matters more than a name check. A tool named `read_program` could be wired
to a sending function if the dispatcher accepted arbitrary callables; it does
not, because the only things it can call are the methods named below. To give an
agent a send capability somebody would have to add a sending method to a protocol
in this file, implement it, add a `ToolSpec` for it, and add that spec to a
toolset — four deliberate edits in three files, each of which fails a test. That
is the opposite of a tool added "temporarily".

R1 IN THE TYPES
===============
Every read returns a frozen snapshot dataclass, never a free dict and never a
model-authored string. The numbers an agent is permitted to state in prose are
exactly the numbers on these snapshots, and `app.agents.grounding` checks that
claim against the generated text rather than trusting it.

CLAUDE.md §3 keeps `domain/` free of I/O; this module is the mirror image — it is
I/O *shaped* but performs none, so the whole agent layer stays testable without a
database.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import JsonValue

from app.core.audit import AuditEvent
from app.domain.enums import ArtifactState, ArtifactType, Persona, ProgramStage, TaskStatus

__all__ = [
    "ContactSnapshot",
    "Draft",
    "DraftSink",
    "DocumentSnapshot",
    "NotADraftError",
    "ProgramReadPort",
    "ProgramSnapshot",
    "RetrievalPort",
    "RetrievedPassage",
    "SavedDraft",
    "SourcingReadPort",
    "TaskSnapshot",
    "TrainerProfileSnapshot",
]


# --- snapshots: what the database said, frozen ------------------------------


@dataclass(frozen=True, slots=True)
class ContactSnapshot:
    """A person a nudge could be addressed to, carrying their persona.

    The persona is not decoration. The supervisor "never contacts an external
    party" (§8), and `app.agents.supervisor.internal_recipients()` enforces that
    by filtering on `persona in INTERNAL_PERSONAS`. A contact with no persona
    cannot be reasoned about, so `persona` is required — an unknown party is
    treated as external by construction rather than by omission.
    """

    contact_id: UUID
    display_name: str
    persona: Persona


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """One open obligation on a program, as the tracker holds it.

    `due_on` is a date, not a datetime: §11 stores UTC and presents IST, and a
    task due "on the 14th" is a business date in IST that no timezone conversion
    should be able to move. Overdue is therefore computed against a date the
    caller passes in (`as_of`), never against `date.today()` inside a node — a
    graph that reads the clock cannot be replayed from a checkpoint and get the
    same answer, which defeats the point of checkpointing it.
    """

    task_id: UUID
    title: str
    status: TaskStatus
    stage: ProgramStage
    due_on: dt.date | None = None
    #: Task ids that must be DONE before this one can start. Empty for a root.
    blocked_by: tuple[UUID, ...] = ()
    #: Who owns it. `None` when the tracker has not assigned it — itself a thing
    #: worth surfacing, and the supervisor reports it rather than guessing.
    owner: ContactSnapshot | None = None


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    """A document obligation — MoU, PO, work order — and whether it is signed."""

    document_id: UUID
    title: str
    #: `app.domain.enums.DocStatus` value. Kept as the enum's `str` value so this
    #: layer does not have to be edited when a new status is added upstream.
    status: str
    signed: bool = False


@dataclass(frozen=True, slots=True)
class ProgramSnapshot:
    """Everything the supervisor needs about one program, read in one go.

    Deliberately a single read rather than a node per field. The supervisor runs
    hourly across every live program; N+1 reads per program is how an hourly job
    becomes a nightly one.

    No commercials here — not `pnl`, not rates, not net pay. §4 walls those off
    from the LDE Executive in the database, and an agent snapshot that carried
    them would be a second copy of commercial data with its own access rules and
    no RLS behind it. The Payout agent (Phase 2 territory, not built here) reads
    money through its own port under the persona of the human who asked.
    """

    program_id: UUID
    college_name: str
    stage: ProgramStage
    program_type: str
    starts_on: dt.date | None = None
    ends_on: dt.date | None = None


@dataclass(frozen=True, slots=True)
class TrainerProfileSnapshot:
    """A sourced candidate profile, for ranking against a requirement spec.

    `years_experience` is a `Decimal` rather than a float — this is not money, so
    R7 does not strictly bind, but a ranking that reorders between runs because
    of binary rounding is a support ticket nobody enjoys.
    """

    profile_id: UUID
    display_name: str
    skills: tuple[str, ...] = ()
    years_experience: Decimal | None = None
    #: Free-text notes from TA. Fed to the model as *context*, never as fact.
    notes: str = ""


@dataclass(frozen=True, slots=True)
class RetrievedPassage:
    """One cited passage. §9: "No citation → no answer."

    `document_title` and `section` are both required, so a passage that cannot be
    cited cannot be constructed. §9 also forbids retrieving structured facts this
    way — RAG supplies policy and context only — which is why there is no numeric
    field on this type at all.
    """

    document_title: str
    section: str
    text: str


# --- drafts: the only thing an agent may write ------------------------------


class NotADraftError(RuntimeError):
    """A `save_draft` result came back in a state other than DRAFT.

    R3 and R4 together: the single write an agent has is a *draft* write, and a
    draft is by definition the first state of the lifecycle. If an implementation
    of `DraftSink` ever returned APPROVED or RELEASED it would mean the agent's
    one write had become a release path, which is the exact failure R3 exists to
    prevent. Raising here makes that a crash rather than a quiet escalation of
    privilege.
    """


@dataclass(frozen=True, slots=True)
class Draft:
    """What an agent proposes. Autonomy level 2: "propose, human edits and sends".

    `payload` is the structured content — the parsed program fields, the
    requirement spec, the ranking. `body` is the prose the model wrote about it.
    They are separate on purpose: R1 says the database owns truth and the LLM owns
    language, so the reviewable facts and the generated sentences do not get
    stirred together into one blob where nobody can tell which is which.

    `grounded_in` records the structured values the body was allowed to quote.
    `app.agents.grounding.assert_grounded` checks the body against it before the
    draft is ever constructed, so a `Draft` in hand has already passed R1.
    """

    artifact_type: ArtifactType
    title: str
    body: str
    payload: Mapping[str, JsonValue]
    #: Concerns which program, when there is one.
    program_id: UUID | None = None
    #: Non-blocking observations for the human reviewer: unusual clauses, missing
    #: fields, low-confidence extractions. Never auto-resolved.
    flags: tuple[str, ...] = ()
    grounded_in: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SavedDraft:
    """A persisted draft. Always DRAFT, and the constructor insists on it.

    This type is small and its `__post_init__` is the whole reason it exists.
    A `DraftSink` implementation is written by another workstream against real
    tables; this check is what stops a plausible-looking implementation from
    handing back an APPROVED artifact and thereby giving the agent layer a
    release path it was never granted (R3, R4).
    """

    artifact_id: UUID
    version: int
    state: ArtifactState = ArtifactState.DRAFT

    def __post_init__(self) -> None:
        if self.state is not ArtifactState.DRAFT:
            raise NotADraftError(
                f"save_draft returned an artifact in {self.state.value}. An agent's only "
                "write is a DRAFT write — CLAUDE.md R3 gives agents no release capability, "
                "and R4 requires DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED with a "
                "human at the approval and release steps."
            )


# --- the protocols ----------------------------------------------------------


@runtime_checkable
class ProgramReadPort(Protocol):
    """Reads about programs and their obligations. Every method is a read.

    `runtime_checkable` so a test can assert an object satisfies it. Note that
    `isinstance` against a runtime-checkable Protocol only checks method
    *presence*, which is why the real guarantee is the dispatcher's closed table
    in `app.agents.tools.dispatch` rather than this decorator.
    """

    async def read_program(self, program_id: UUID) -> ProgramSnapshot | None:
        """The program, or `None` when the caller may not reach it (§4 RLS)."""
        ...

    async def list_program_tasks(self, program_id: UUID) -> Sequence[TaskSnapshot]:
        """Every task on the program, in any status. Filtering is the caller's."""
        ...

    async def list_program_documents(self, program_id: UUID) -> Sequence[DocumentSnapshot]:
        """Document obligations and their signature status."""
        ...

    async def list_internal_contacts(self, program_id: UUID) -> Sequence[ContactSnapshot]:
        """People associated with the program, each carrying a persona.

        Named "internal" for what the supervisor does with it, not for what it
        returns: the port may hand back trainers and college contacts, and
        `internal_recipients()` drops them. Filtering in the consumer rather than
        trusting the provider means a future implementation that forgets the
        filter cannot make the supervisor address a college.
        """
        ...


@runtime_checkable
class SourcingReadPort(Protocol):
    """Reads the Sourcing Liaison needs on top of `ProgramReadPort` (§8)."""

    async def list_candidate_profiles(self, program_id: UUID) -> Sequence[TrainerProfileSnapshot]:
        """Profiles TA has submitted against this program's requirement."""
        ...

    async def read_requirement_spec(self, program_id: UUID) -> Mapping[str, JsonValue] | None:
        """The current requirement spec, for the re-spec diff. `None` if none yet."""
        ...


@runtime_checkable
class RetrievalPort(Protocol):
    """The shared RAG layer (§8, §9). Persona filter applies BEFORE retrieval."""

    async def search_corpus(
        self, corpus: str, query: str, limit: int = 5
    ) -> Sequence[RetrievedPassage]:
        """Cited passages only. §9: no citation, no answer."""
        ...


@runtime_checkable
class DraftSink(Protocol):
    """The one write. R3's "and `save_draft` only", as a type.

    `save_draft` takes the `AuditEvent` alongside the draft and the implementation
    persists BOTH IN ONE TRANSACTION. That mirrors
    `app.services.approval.state_machine.ApprovalOutcome`, and for the same
    reason §11 gives: the audit row is built by the code that decided the write
    was legitimate, not by whoever remembered to log afterwards. An implementation
    that commits the draft and drops the event is a defect.
    """

    async def save_draft(self, draft: Draft, event: AuditEvent) -> SavedDraft:
        """Persist a DRAFT artifact and its audit row atomically."""
        ...
