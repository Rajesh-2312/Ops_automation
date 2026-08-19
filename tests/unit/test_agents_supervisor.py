"""The Program Orchestrator. CLAUDE.md §8.

    "not a chatbot. Runs hourly and on events. Per program: what phase, what
     tasks open, what overdue, what blocked, who to nudge. Routes to
     specialists. Never contacts an external party."

Each clause is asserted here, and the two that carry risk get the most attention:
the assessment is deterministic (no model, no clock), and no nudge can be
addressed to a trainer or a college.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import supervisor as supervisor_module
from app.agents.checkpointer import in_memory_checkpointer, program_thread_config
from app.agents.supervisor import (
    STAGE_ROUTES,
    SpecialistReport,
    SupervisorState,
    assess,
    build_supervisor_graph,
    internal_recipients,
    route_for_stage,
)
from app.agents.tools import AgentName, PortBundle, toolset_for
from app.domain.enums import INTERNAL_PERSONAS, Persona, ProgramStage, TaskStatus
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeProgramPort,
    a_contact,
    a_program,
    a_task,
)

TODAY = dt.date(2026, 8, 15)
YESTERDAY = dt.date(2026, 8, 14)
TOMORROW = dt.date(2026, 8, 16)


# --- "what phase, what open, what overdue, what blocked" ---------------------


def test_done_tasks_are_not_open() -> None:
    tasks = [a_task("done", status=TaskStatus.DONE), a_task("todo")]
    result = assess(a_program(), tasks, [], TODAY)
    assert [task.title for task in result.open_tasks] == ["todo"]


def test_blocked_counts_as_open() -> None:
    """It is not finished, and surfacing it is the entire point."""
    result = assess(a_program(), [a_task("stuck", status=TaskStatus.BLOCKED)], [], TODAY)
    assert len(result.open_tasks) == 1
    assert len(result.blocked_tasks) == 1


def test_overdue_is_measured_against_the_supplied_date() -> None:
    """No node reads the clock; a checkpointed graph must replay identically."""
    tasks = [a_task("late", due_on=YESTERDAY), a_task("soon", due_on=TOMORROW)]
    result = assess(a_program(), tasks, [], TODAY)
    assert [task.title for task in result.overdue_tasks] == ["late"]


def test_a_task_due_today_is_not_yet_overdue() -> None:
    result = assess(a_program(), [a_task("today", due_on=TODAY)], [], TODAY)
    assert result.overdue_tasks == ()


def test_an_undated_task_is_unscheduled_not_overdue() -> None:
    """Otherwise importing a backlog fills the dashboard with false alarms."""
    result = assess(a_program(), [a_task("someday", due_on=None)], [], TODAY)
    assert result.overdue_tasks == ()


def test_an_unmet_dependency_blocks_even_when_the_status_has_not_caught_up() -> None:
    """The dependency graph is the truth; the status column is a cache of it."""
    predecessor = a_task("sign WO", status=TaskStatus.PENDING)
    dependent = a_task("deploy", blocked_by=(predecessor.task_id,))
    result = assess(a_program(), [predecessor, dependent], [], TODAY)
    assert [task.title for task in result.blocked_tasks] == ["deploy"]


def test_a_met_dependency_does_not_block() -> None:
    predecessor = a_task("sign WO", status=TaskStatus.DONE)
    dependent = a_task("deploy", blocked_by=(predecessor.task_id,))
    result = assess(a_program(), [predecessor, dependent], [], TODAY)
    assert result.blocked_tasks == ()


def test_unsigned_documents_are_surfaced() -> None:
    from app.agents.ports import DocumentSnapshot

    documents = [
        DocumentSnapshot(document_id=uuid4(), title="MoU", status="signed", signed=True),
        DocumentSnapshot(document_id=uuid4(), title="WO", status="sent", signed=False),
    ]
    result = assess(a_program(), [], documents, TODAY)
    assert [doc.title for doc in result.unsigned_documents] == ["WO"]
    assert result.needs_attention


def test_a_clean_program_needs_no_attention() -> None:
    result = assess(a_program(), [a_task("todo", due_on=TOMORROW)], [], TODAY)
    assert not result.needs_attention


# --- "who to nudge" — and never an external party ----------------------------


@pytest.mark.parametrize("persona", sorted(INTERNAL_PERSONAS))
def test_internal_owners_are_nudged(persona: Persona) -> None:
    task = a_task("late", due_on=YESTERDAY, owner=a_contact(persona))
    result = assess(a_program(), [task], [], TODAY)
    assert len(result.nudges) == 1
    assert result.nudges[0].contact.persona is persona


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
def test_external_owners_are_never_nudged(persona: Persona) -> None:
    """§8: "Never contacts an external party." Not even as a dashboard row."""
    task = a_task("late", due_on=YESTERDAY, owner=a_contact(persona, "Outsider"))
    result = assess(a_program(), [task], [], TODAY)
    assert result.nudges == ()
    assert [t.title for t in result.externally_owned_tasks] == ["late"]


def test_an_unowned_task_is_surfaced_not_guessed() -> None:
    task = a_task("late", due_on=YESTERDAY, owner=None)
    result = assess(a_program(), [task], [], TODAY)
    assert result.nudges == ()
    assert [t.title for t in result.unassigned_tasks] == ["late"]


def test_no_nudge_in_any_mixed_program_reaches_an_external_persona() -> None:
    """The property, asserted over a mixed population rather than one case."""
    tasks = [
        a_task(f"late-{persona.value}", due_on=YESTERDAY, owner=a_contact(persona))
        for persona in Persona
    ]
    result = assess(a_program(), tasks, [], TODAY)
    assert result.nudges
    for nudge in result.nudges:
        assert nudge.contact.persona in INTERNAL_PERSONAS


def test_a_task_both_overdue_and_blocked_is_chased_once() -> None:
    """Two rows about one task trains people to ignore the dashboard."""
    predecessor = a_task("sign WO", status=TaskStatus.PENDING)
    dependent = a_task(
        "deploy",
        due_on=YESTERDAY,
        blocked_by=(predecessor.task_id,),
        owner=a_contact(Persona.MANAGER),
    )
    result = assess(a_program(), [predecessor, dependent], [], TODAY)
    chased = [nudge for nudge in result.nudges if nudge.task_id == dependent.task_id]
    assert len(chased) == 1
    assert chased[0].reason == "blocked"


def test_internal_recipients_splits_the_three_cases() -> None:
    nudges, unassigned, external = internal_recipients(
        [
            a_task("a", owner=a_contact(Persona.LDE_EXECUTIVE)),
            a_task("b", owner=None),
            a_task("c", owner=a_contact(Persona.COLLEGE)),
        ],
        "overdue",
    )
    assert len(nudges) == 1 and len(unassigned) == 1 and len(external) == 1


# --- "not a chatbot": no model, no clock -------------------------------------


def test_the_supervisor_module_imports_no_llm() -> None:
    """Phase, open, overdue and blocked are facts. R1 puts facts on the DB side.

    Asserted over the import graph rather than the source text, so that a
    docstring may still *discuss* `app.core.llm` — as this module's does, to
    explain why its routing table is flat — without the check misfiring.
    """
    source = Path(inspect.getfile(supervisor_module)).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)

    assert not [name for name in imported if "llm" in name.lower()]
    for banned in ("app.core.llm", "app.agents.runtime.Completer", "app.agents.runtime"):
        assert banned not in imported


def test_the_supervisor_holds_no_model_client_at_runtime() -> None:
    """Nothing in the module's namespace can complete a prompt."""
    for name in ("LLMTask", "Completer", "LLMClient", "AgentRuntime"):
        assert not hasattr(supervisor_module, name)


def test_the_supervisor_state_has_no_message_list() -> None:
    """A `messages` key is how an orchestrator quietly becomes a chatbot."""
    assert "messages" not in SupervisorState.__annotations__


def test_assess_is_pure_of_the_clock() -> None:
    """Same inputs, same answer — the precondition for replaying a checkpoint."""
    tasks = [a_task("late", due_on=YESTERDAY, owner=a_contact(Persona.MANAGER))]
    first = assess(a_program(), tasks, [], TODAY)
    second = assess(a_program(), tasks, [], TODAY)
    assert first == second


def test_the_supervisor_holds_no_write_capability() -> None:
    """It cannot even draft, so there is nothing for it to have sent."""
    assert not toolset_for(AgentName.SUPERVISOR).can_write


# --- routing -----------------------------------------------------------------


def test_every_stage_routes_somewhere() -> None:
    """A new stage with no owner must fail loudly, not fall to a default."""
    assert set(STAGE_ROUTES) == set(ProgramStage)
    for stage in ProgramStage:
        assert isinstance(route_for_stage(stage), AgentName)


def test_routing_is_not_a_judgement_call() -> None:
    assert route_for_stage(ProgramStage.TRAINER_SOURCING) is AgentName.SOURCING
    assert route_for_stage(ProgramStage.ACQUISITION_SETUP) is AgentName.INTAKE


# --- the graph ---------------------------------------------------------------


def a_port(stage: ProgramStage = ProgramStage.TRAINER_SOURCING) -> FakeProgramPort:
    return FakeProgramPort(
        program=a_program(stage),
        tasks=(a_task("late", due_on=YESTERDAY, owner=a_contact(Persona.MANAGER)),),
    )


async def test_the_graph_routes_to_the_stage_owner() -> None:
    called: list[str] = []

    async def sourcing(state: SupervisorState) -> SpecialistReport:
        called.append("sourcing")
        return SpecialistReport(notes=("spec drafted",))

    graph = build_supervisor_graph(
        PortBundle(programs=a_port(ProgramStage.TRAINER_SOURCING)),
        checkpointer=in_memory_checkpointer(),
        specialists={AgentName.SOURCING: sourcing},
    )
    result = await graph.ainvoke(
        {"program_id": PROGRAM_ID, "as_of": TODAY}, config=program_thread_config(PROGRAM_ID)
    )
    assert called == ["sourcing"]
    assert result["routed_to"] == (AgentName.SOURCING,)
    assert "spec drafted" in result["notes"]


async def test_an_unimplemented_specialist_defers_rather_than_improvising() -> None:
    """§13 ships two agents. The rest are named and deferred, not substituted."""
    graph = build_supervisor_graph(
        PortBundle(programs=a_port(ProgramStage.CLOSEOUT_FINANCE)),
        checkpointer=in_memory_checkpointer(),
        specialists={},
    )
    result = await graph.ainvoke(
        {"program_id": PROGRAM_ID, "as_of": TODAY}, config=program_thread_config(PROGRAM_ID)
    )
    assert result["routed_to"] == ()
    assert any("payout" in note and "not implemented" in note for note in result["notes"])


async def test_a_clean_program_routes_nowhere() -> None:
    """The hourly sweep must not wake a specialist for a program that is fine."""
    called: list[str] = []

    async def sourcing(state: SupervisorState) -> SpecialistReport:
        called.append("sourcing")
        return SpecialistReport()

    port = FakeProgramPort(
        program=a_program(ProgramStage.TRAINER_SOURCING),
        tasks=(a_task("fine", due_on=TOMORROW),),
    )
    graph = build_supervisor_graph(
        PortBundle(programs=port),
        checkpointer=in_memory_checkpointer(),
        specialists={AgentName.SOURCING: sourcing},
    )
    result = await graph.ainvoke(
        {"program_id": PROGRAM_ID, "as_of": TODAY}, config=program_thread_config(PROGRAM_ID)
    )
    assert called == []
    assert result["routed_to"] == ()


async def test_an_unreachable_program_does_not_kill_the_sweep() -> None:
    """RLS (§4) or a deleted row — either way, one program must not fail the run."""
    graph = build_supervisor_graph(
        PortBundle(programs=FakeProgramPort(program=None)),
        checkpointer=in_memory_checkpointer(),
    )
    result = await graph.ainvoke(
        {"program_id": PROGRAM_ID, "as_of": TODAY}, config=program_thread_config(PROGRAM_ID)
    )
    assert result["assessment"] is None


async def test_a_specialist_cannot_overwrite_the_assessment() -> None:
    """A specialist reports; it does not edit the facts the supervisor read (R1)."""

    async def liar(state: SupervisorState) -> SpecialistReport:
        return SpecialistReport(notes=("all clear",))

    graph = build_supervisor_graph(
        PortBundle(programs=a_port()),
        checkpointer=in_memory_checkpointer(),
        specialists={AgentName.SOURCING: liar},
    )
    result = await graph.ainvoke(
        {"program_id": PROGRAM_ID, "as_of": TODAY}, config=program_thread_config(PROGRAM_ID)
    )
    assert result["assessment"] is not None
    assert result["assessment"].overdue_tasks, "the specialist must not have erased this"


async def test_the_graph_reads_through_the_r3_gated_toolset() -> None:
    """The supervisor uses tools, not raw ports — the same gate a specialist has."""
    port = a_port()
    graph = build_supervisor_graph(PortBundle(programs=port), checkpointer=in_memory_checkpointer())
    await graph.ainvoke(
        {"program_id": PROGRAM_ID, "as_of": TODAY}, config=program_thread_config(PROGRAM_ID)
    )
    assert port.reads == ["read_program", "list_program_tasks", "list_program_documents"]


# --- the checkpointer: pausing for days --------------------------------------


async def test_state_survives_between_hourly_ticks_on_one_thread() -> None:
    """§8: "a Postgres checkpointer so a program graph can pause for days".

    In-memory here, but the property under test is the same one: the second tick
    resumes the program's own thread rather than starting a fresh graph.
    """
    checkpointer = in_memory_checkpointer()
    graph = build_supervisor_graph(PortBundle(programs=a_port()), checkpointer=checkpointer)
    config = program_thread_config(PROGRAM_ID)

    await graph.ainvoke({"program_id": PROGRAM_ID, "as_of": TODAY}, config=config)
    snapshot = await graph.aget_state(config)
    assert snapshot.values["assessment"] is not None
    assert snapshot.values["program"].program_id == PROGRAM_ID


async def test_two_programs_do_not_share_a_thread() -> None:
    other = uuid4()
    assert program_thread_config(PROGRAM_ID) != program_thread_config(other)
