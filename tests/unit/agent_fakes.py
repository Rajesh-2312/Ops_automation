"""Fakes for the agent tests. No database, no network, no OpenRouter.

Two things this module exists to make possible.

**A mocked LLM.** `FakeLLM` implements `app.agents.runtime.Completer` and returns
queued responses. Tests state exactly what the model says, which is the only way
to assert what happens when it says something wrong — an invented figure, a
malformed extraction, a hallucinated tool. A test that called a real model could
not reproduce those on demand, and §12's "assert [...] absence of fabricated
figures" is precisely an assertion about the wrong case.

**Fake ports.** Phase 4 owns no schema (see `app.agents.ports`), so the ports are
protocols and these are the implementations the tests run against. They are
deliberately dumb: they return what they were constructed with. A fake with logic
in it ends up asserting its own behaviour.

`FakeDraftSink` records the `(draft, event)` pairs it was given, which is how the
§11 audit assertions are made without a database.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from pydantic import JsonValue

from app.agents.ports import (
    ContactSnapshot,
    DocumentSnapshot,
    Draft,
    ProgramSnapshot,
    RetrievedPassage,
    SavedDraft,
    TaskSnapshot,
    TrainerProfileSnapshot,
)
from app.core.audit import AuditEvent
from app.core.llm import LLMResponse
from app.domain.enums import LLMTask, ModelTier, Persona, ProgramStage, TaskStatus

PROGRAM_ID = UUID("11111111-1111-1111-1111-111111111111")


@dataclass
class FakeLLM:
    """A queued-response `Completer`. Records every call for §11 assertions."""

    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)
    prompt_tokens: int = 120
    completion_tokens: int = 40
    latency_ms: int = 7

    async def complete(
        self,
        task: LLMTask,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if not self.responses:
            raise AssertionError(
                f"FakeLLM ran out of queued responses on a {task.value} call. "
                "Queue one response per expected model call."
            )
        text = self.responses.pop(0)
        self.calls.append({"task": task, "system": system, "user": user})
        # The tier is derived the same way `app.core.llm` derives it, so a test can
        # assert routing (§2) without importing the routing table into the fake.
        tier = (
            ModelTier.FRONTIER
            if task in {LLMTask.EXTRACTION, LLMTask.GOVERNANCE_REPORT}
            else ModelTier.VOLUME
        )
        return LLMResponse(
            text=text,
            model=f"fake/{tier.value}",
            tier=tier,
            task=task,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            latency_ms=self.latency_ms,
        )

    @property
    def tasks_called(self) -> list[LLMTask]:
        return [call["task"] for call in self.calls]  # type: ignore[misc]


@dataclass
class FakeProgramPort:
    """A `ProgramReadPort` that returns what it was constructed with."""

    program: ProgramSnapshot | None = None
    tasks: Sequence[TaskSnapshot] = ()
    documents: Sequence[DocumentSnapshot] = ()
    contacts: Sequence[ContactSnapshot] = ()
    reads: list[str] = field(default_factory=list)

    async def read_program(self, program_id: UUID) -> ProgramSnapshot | None:
        self.reads.append("read_program")
        return self.program

    async def list_program_tasks(self, program_id: UUID) -> Sequence[TaskSnapshot]:
        self.reads.append("list_program_tasks")
        return self.tasks

    async def list_program_documents(self, program_id: UUID) -> Sequence[DocumentSnapshot]:
        self.reads.append("list_program_documents")
        return self.documents

    async def list_internal_contacts(self, program_id: UUID) -> Sequence[ContactSnapshot]:
        self.reads.append("list_internal_contacts")
        return self.contacts


@dataclass
class FakeSourcingPort:
    """A `SourcingReadPort`."""

    profiles: Sequence[TrainerProfileSnapshot] = ()
    spec: Mapping[str, JsonValue] | None = None

    async def list_candidate_profiles(self, program_id: UUID) -> Sequence[TrainerProfileSnapshot]:
        return self.profiles

    async def read_requirement_spec(self, program_id: UUID) -> Mapping[str, JsonValue] | None:
        return self.spec


@dataclass
class FakeRetrievalPort:
    """A `RetrievalPort`. Every passage carries a citation (§9)."""

    passages: Sequence[RetrievedPassage] = ()

    async def search_corpus(
        self, corpus: str, query: str, limit: int = 5
    ) -> Sequence[RetrievedPassage]:
        return list(self.passages)[:limit]


@dataclass
class FakeDraftSink:
    """A `DraftSink` that records what it was asked to persist.

    Returns a `SavedDraft`, which can only ever be DRAFT — the type refuses any
    other state, so this fake cannot accidentally model a release path.
    """

    saved: list[tuple[Draft, AuditEvent]] = field(default_factory=list)

    async def save_draft(self, draft: Draft, event: AuditEvent) -> SavedDraft:
        self.saved.append((draft, event))
        return SavedDraft(artifact_id=uuid4(), version=1)


# --- convenience builders ----------------------------------------------------


def a_program(
    stage: ProgramStage = ProgramStage.TRAINER_SOURCING,
    *,
    college_name: str = "Malineni Lakshmaiah",
    program_type: str = "bCAP",
) -> ProgramSnapshot:
    return ProgramSnapshot(
        program_id=PROGRAM_ID,
        college_name=college_name,
        stage=stage,
        program_type=program_type,
        starts_on=dt.date(2026, 7, 1),
        ends_on=dt.date(2026, 12, 31),
    )


def a_contact(persona: Persona, name: str = "R. Maroju") -> ContactSnapshot:
    return ContactSnapshot(contact_id=uuid4(), display_name=name, persona=persona)


def a_task(
    title: str = "Chase work order",
    *,
    status: TaskStatus = TaskStatus.PENDING,
    stage: ProgramStage = ProgramStage.TRAINER_SOURCING,
    due_on: dt.date | None = None,
    blocked_by: tuple[UUID, ...] = (),
    owner: ContactSnapshot | None = None,
    task_id: UUID | None = None,
) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id or uuid4(),
        title=title,
        status=status,
        stage=stage,
        due_on=due_on,
        blocked_by=blocked_by,
        owner=owner,
    )
