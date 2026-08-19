"""The Program Orchestrator. CLAUDE.md §8 is its specification.

    "Supervisor (Program Orchestrator) — not a chatbot. Runs hourly and on
     events. Per program: what phase, what tasks open, what overdue, what
     blocked, who to nudge. Routes to specialists. Never contacts an external
     party."

Every clause of that paragraph is a design constraint, and each one shows up
below as something the code cannot do rather than something it declines to do.

**"Not a chatbot."** There is no message list in the state and no LLM call
anywhere in this module. Read the imports: no `Completer`, no `LLMTask`. Phase,
open, overdue and blocked are *facts*, and R1 puts facts on the database side of
the line. §8 makes the same point about the Escalation Engine — "deterministic
SLA rules. Not LLM judgement." An orchestrator that asked a model which tasks
were overdue would be asking a model to invent a fact it could have read.

**"Runs hourly."** `assess()` takes `as_of` as an argument and no node reads the
clock. A graph that calls `date.today()` inside a node cannot be replayed from a
checkpoint and produce the same answer, which makes a checkpointed graph
pointless and a bug report unreproducible.

**"Routes to specialists."** Routing is a lookup from program stage in
`STAGE_ROUTES`, a flat table transcribed from §8's agent list. Specialists that
Phase 4 has not built (§13 ships Intake and Sourcing) route to `defer`, which
records the intended handler and stops. It does not silently do nothing, and it
does not improvise a substitute.

**"Never contacts an external party."** Three independent reasons this holds:
the supervisor's toolset (`SUPERVISOR_TOOLS`) contains no write tool at all — not
even `save_draft`, so it cannot even draft a message; `internal_recipients()`
filters every nudge target down to `INTERNAL_PERSONAS`, dropping trainers and
college contacts; and R3 means no send capability exists anywhere in the layer
regardless. A nudge here is a row on an internal dashboard naming a colleague,
which is the strongest form of "contact" this agent has.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Protocol, TypedDict
from uuid import UUID

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.agents.ports import ContactSnapshot, DocumentSnapshot, ProgramSnapshot, TaskSnapshot
from app.agents.tools.catalog import AgentName, toolset_for
from app.agents.tools.dispatch import PortBundle, ToolDispatcher, bind
from app.domain.enums import INTERNAL_PERSONAS, ProgramStage, TaskStatus

__all__ = [
    "OPEN_STATUSES",
    "STAGE_ROUTES",
    "Nudge",
    "ProgramAssessment",
    "SpecialistHandler",
    "SpecialistReport",
    "SupervisorState",
    "assess",
    "build_supervisor_graph",
    "internal_recipients",
    "route_for_stage",
]

_log = structlog.get_logger(__name__)


#: A task that still needs doing. BLOCKED counts as open — it is not finished,
#: and the whole reason to surface it is that somebody must unblock it.
OPEN_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
)

#: Program stage -> the specialist that owns work at that stage (§3's stage list
#: against §8's agent table). A flat table rather than logic, for the reason
#: `app.core.llm.TASK_TIER` gives: routing policy should be readable at a glance
#: and arguable without reading code.
STAGE_ROUTES: Final[Mapping[ProgramStage, AgentName]] = {
    ProgramStage.ACQUISITION_SETUP: AgentName.INTAKE,
    ProgramStage.TRAINER_SOURCING: AgentName.SOURCING,
    ProgramStage.TRAINER_ONBOARDING: AgentName.ONBOARDING,
    ProgramStage.DEPLOYMENT: AgentName.LOGISTICS,
    ProgramStage.ACTIVE_MONITORING: AgentName.MONITOR,
    ProgramStage.CLOSEOUT_FINANCE: AgentName.PAYOUT,
}


# --- the assessment: pure, deterministic, no model involved -----------------


@dataclass(frozen=True, slots=True)
class Nudge:
    """One internal person to chase, and what about.

    Constructed only by `internal_recipients()`, which is the persona filter. The
    type carries the persona so a reviewer of a rendered dashboard can see that
    every row is internal without trusting the code that produced it.
    """

    contact: ContactSnapshot
    task_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class ProgramAssessment:
    """§8's four questions, answered from the database and nothing else.

    Every field is derived from `TaskSnapshot`s the port returned. No field is
    generated, estimated, or scored by a model — see the module docstring.
    """

    program_id: UUID
    stage: ProgramStage
    as_of: dt.date
    open_tasks: tuple[TaskSnapshot, ...]
    overdue_tasks: tuple[TaskSnapshot, ...]
    blocked_tasks: tuple[TaskSnapshot, ...]
    unsigned_documents: tuple[DocumentSnapshot, ...]
    nudges: tuple[Nudge, ...]
    #: Open tasks nobody owns, and open tasks owned by an external party. Both are
    #: surfaced rather than nudged: the supervisor will not guess an owner, and it
    #: will not address a trainer or a college. A human picks these up.
    unassigned_tasks: tuple[TaskSnapshot, ...]
    externally_owned_tasks: tuple[TaskSnapshot, ...]

    @property
    def needs_attention(self) -> bool:
        return bool(self.overdue_tasks or self.blocked_tasks or self.unsigned_documents)


def internal_recipients(
    tasks: Sequence[TaskSnapshot], reason: str
) -> tuple[tuple[Nudge, ...], tuple[TaskSnapshot, ...], tuple[TaskSnapshot, ...]]:
    """Split tasks into (nudges for internal owners, unowned, externally owned).

    §8: the supervisor "never contacts an external party". `INTERNAL_PERSONAS` is
    Senior Manager, Manager and LDE Executive; a trainer or a college contact is
    filtered out here and reported separately for a human to handle.

    Filtering in the consumer rather than trusting the port to return only
    internal contacts is deliberate — a future `ProgramReadPort` implementation
    that forgets the filter must not be able to make this agent address a college.
    """
    nudges: list[Nudge] = []
    unassigned: list[TaskSnapshot] = []
    external: list[TaskSnapshot] = []
    for task in tasks:
        owner = task.owner
        if owner is None:
            unassigned.append(task)
        elif owner.persona in INTERNAL_PERSONAS:
            nudges.append(Nudge(contact=owner, task_id=task.task_id, reason=reason))
        else:
            external.append(task)
    return tuple(nudges), tuple(unassigned), tuple(external)


def assess(
    program: ProgramSnapshot,
    tasks: Sequence[TaskSnapshot],
    documents: Sequence[DocumentSnapshot],
    as_of: dt.date,
) -> ProgramAssessment:
    """Answer §8's four questions for one program. Pure — no I/O, no clock, no LLM.

    "Blocked" is two things at once, and both matter: a task the tracker marked
    BLOCKED, and a task whose `blocked_by` predecessors are not all DONE. The
    second catches the case the tracker has not caught up with yet, which is the
    common one on an hourly sweep — the dependency graph is the truth and the
    status column is a cache of it.

    Overdue is computed only for tasks that carry a `due_on`. A task with no due
    date is not overdue; it is unscheduled, and pretending otherwise would fill
    the dashboard with noise on the day someone imports a backlog.
    """
    done_ids = {task.task_id for task in tasks if task.status is TaskStatus.DONE}
    open_tasks = tuple(task for task in tasks if task.status in OPEN_STATUSES)

    overdue = tuple(task for task in open_tasks if task.due_on is not None and task.due_on < as_of)
    blocked = tuple(
        task
        for task in open_tasks
        if task.status is TaskStatus.BLOCKED
        or any(dependency not in done_ids for dependency in task.blocked_by)
    )
    unsigned = tuple(document for document in documents if not document.signed)

    # A task that is both overdue and blocked is chased once, for the blocking
    # reason — "unblock this" is the actionable half, and two rows about one task
    # is how a dashboard trains people to ignore it.
    blocked_ids = {task.task_id for task in blocked}
    chase_blocked, unassigned_b, external_b = internal_recipients(blocked, "blocked")
    chase_overdue, unassigned_o, external_o = internal_recipients(
        [task for task in overdue if task.task_id not in blocked_ids], "overdue"
    )

    return ProgramAssessment(
        program_id=program.program_id,
        stage=program.stage,
        as_of=as_of,
        open_tasks=open_tasks,
        overdue_tasks=overdue,
        blocked_tasks=blocked,
        unsigned_documents=unsigned,
        nudges=chase_blocked + chase_overdue,
        unassigned_tasks=unassigned_b + unassigned_o,
        externally_owned_tasks=external_b + external_o,
    )


def route_for_stage(stage: ProgramStage) -> AgentName:
    """Which specialist owns work at this stage. Total over `ProgramStage`.

    A `KeyError` here means a stage was added to `app.domain.enums` without
    anybody deciding who handles it. That is the correct failure: the alternative
    — a default route — silently hands a new stage to whichever agent happened to
    be first in the table.
    """
    return STAGE_ROUTES[stage]


# --- the graph --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpecialistReport:
    """What a specialist hands back to the supervisor. Deliberately almost nothing.

    A specialist reports; it does not edit the orchestrator's view of the world.
    Returning a narrow type rather than a partial state update means a specialist
    cannot overwrite `assessment`, `program` or `tasks` — the facts the supervisor
    read from the database (R1). If it could, one buggy specialist could rewrite
    the program state every other branch is reasoning about, and the audit trail
    would attribute the change to the supervisor.

    `drafts` records artifact ids the specialist saved, so the orchestration trail
    can point at them. It carries ids, not content: the draft itself lives in its
    own table under its own access rules (§4).
    """

    notes: tuple[str, ...] = ()
    drafts: tuple[UUID, ...] = ()


#: A specialist entry point, as the supervisor sees it. Takes the supervisor's
#: state, returns a report. Specialists own their own runtimes, toolsets and LLM
#: clients; the supervisor knows only this signature, which is why it cannot
#: reach into a specialist's capabilities or borrow its draft sink.
SpecialistHandler = Callable[["SupervisorState"], Awaitable[SpecialistReport]]


def _merge(left: tuple[Any, ...], right: tuple[Any, ...]) -> tuple[Any, ...]:
    """Reducer for fields several branches may write concurrently.

    Conditional fan-out can route one tick to more than one specialist. Without a
    reducer LangGraph raises `InvalidUpdateError` on the second concurrent write;
    with `operator.add` semantics the notes from every branch survive, which is
    what an operator dashboard wants.
    """
    return (*left, *right)


class SupervisorState(TypedDict, total=False):
    """The checkpointed state of one program's orchestration thread.

    `total=False` because nodes contribute their own slices — LangGraph merges
    partial updates. `program_id` and `as_of` are supplied by the caller at
    invocation; everything else is filled in as the graph runs.

    Note there is no `messages` key. This is not a chatbot (§8), and adding a
    message list is how it would quietly become one.
    """

    program_id: UUID
    as_of: dt.date
    program: ProgramSnapshot | None
    tasks: tuple[TaskSnapshot, ...]
    documents: tuple[DocumentSnapshot, ...]
    assessment: ProgramAssessment | None
    routed_to: Annotated[tuple[AgentName, ...], _merge]
    notes: Annotated[tuple[str, ...], _merge]


def build_supervisor_graph(
    ports: PortBundle,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    specialists: Mapping[AgentName, SpecialistHandler] | None = None,
) -> Any:  # noqa: ANN401  # langgraph's CompiledStateGraph generics vary by version
    """Compile the orchestrator graph.

        START -> load -> assess -> (conditional) -> specialist | defer -> END

    `checkpointer` should be `app.agents.checkpointer.postgres_checkpointer()` in
    production (§8). It is optional so a caller can run one stateless assessment —
    a dashboard refresh, say — without writing a checkpoint for it, and so tests
    can use `in_memory_checkpointer()`. It is not defaulted to in-memory: a silent
    non-durable default is how "pauses for days" turns into "forgets on redeploy".

    `specialists` is what the supervisor may route *to*. Absent entries route to
    `defer`, which records the intended handler and ends.

    All nine specialists in §8 now exist as modules. That does NOT make them all
    routable: this map is supplied by the caller, deliberately, so which agents
    are live is a deployment decision rather than an import side effect. §13
    promotes one agent at a time with rollback, and a registry that wired itself
    up on import would take that choice away. Passing a subset is the supported
    case, and anything absent still defers.

    The supervisor's own dispatcher is built from `SUPERVISOR_TOOLS`, so every
    read below goes through the R3 toolset gate exactly as a specialist's would.
    """
    dispatcher = bind(toolset_for(AgentName.SUPERVISOR), ports)
    handlers = dict(specialists or {})

    async def load(state: SupervisorState) -> SupervisorState:
        """Read the program, its tasks and its documents. R1: facts come from here."""
        program_id = state["program_id"]
        program = await dispatcher.read_program(program_id)
        if program is None:
            # Either the program does not exist or RLS put it out of reach (§4).
            # Both are "nothing to orchestrate", and neither is an exception: the
            # hourly sweep must not die on one unreachable program.
            _log.info("supervisor.program_unreachable", program_id=str(program_id))
            return {"program": None, "tasks": (), "documents": ()}
        return {
            "program": program,
            "tasks": tuple(await dispatcher.list_program_tasks(program_id)),
            "documents": tuple(await dispatcher.list_program_documents(program_id)),
        }

    async def assess_node(state: SupervisorState) -> SupervisorState:
        """Apply the deterministic assessment. No model, no clock — see `assess`."""
        program = state.get("program")
        if program is None:
            return {"assessment": None}
        assessment = assess(
            program,
            state.get("tasks", ()),
            state.get("documents", ()),
            state["as_of"],
        )
        _log.info(
            "supervisor.assessed",
            program_id=str(assessment.program_id),
            stage=assessment.stage.value,
            open=len(assessment.open_tasks),
            overdue=len(assessment.overdue_tasks),
            blocked=len(assessment.blocked_tasks),
            unsigned_documents=len(assessment.unsigned_documents),
            nudges=len(assessment.nudges),
        )
        return {"assessment": assessment}

    async def defer(state: SupervisorState) -> SupervisorState:
        """Record an intended route with no implementation behind it.

        Not a failure and not a no-op: the intended handler is named in `notes`
        so an operator can see that the program is sitting at a stage nothing
        automates yet, which is a fact worth showing rather than hiding.
        """
        assessment = state.get("assessment")
        if assessment is None:
            return {"notes": ("no assessment: program unreachable or out of scope",)}
        agent = route_for_stage(assessment.stage)
        return {
            "notes": (
                f"stage {assessment.stage.value} routes to {agent.value}, "
                "which is not implemented in this phase; no action taken",
            )
        }

    def choose(state: SupervisorState) -> str:
        """Conditional edge. Deterministic, and total over the routing table."""
        assessment = state.get("assessment")
        if assessment is None or not assessment.needs_attention:
            return "end"
        agent = route_for_stage(assessment.stage)
        return agent.value if agent in handlers else "defer"

    graph: StateGraph[SupervisorState, None, SupervisorState, SupervisorState] = StateGraph(
        SupervisorState
    )
    graph.add_node("load", load)
    graph.add_node("assess", assess_node)
    graph.add_node("defer", defer)
    graph.add_edge(START, "load")
    graph.add_edge("load", "assess")

    branches: dict[Hashable, str] = {"end": END, "defer": "defer"}
    for agent, handler in handlers.items():
        graph.add_node(agent.value, _specialist_node(agent, handler))
        graph.add_edge(agent.value, END)
        branches[agent.value] = agent.value

    graph.add_conditional_edges("assess", choose, branches)
    graph.add_edge("defer", END)
    return graph.compile(checkpointer=checkpointer)


class _GraphNode(Protocol):
    """A LangGraph node over `SupervisorState`.

    Spelled as a Protocol rather than a `Callable[...]` alias because LangGraph's
    own `_Node` protocol declares its parameter by name (`state`), and a bare
    `Callable` contributes an anonymous positional parameter that does not
    satisfy it. Matching the library's shape here keeps `add_node` type-checked
    instead of silenced with an ignore.
    """

    async def __call__(self, state: SupervisorState) -> SupervisorState: ...


def _specialist_node(agent: AgentName, handler: SpecialistHandler) -> _GraphNode:
    """Wrap a specialist so every route it takes is recorded in `routed_to`.

    The record is written by the supervisor, not reported by the specialist, so a
    specialist cannot run without the orchestration trail showing that it did
    (§11 — every state transition is accounted for). This is also the only place
    `routed_to` is ever written.
    """

    async def node(state: SupervisorState) -> SupervisorState:
        _log.info(
            "supervisor.routed",
            agent=agent.value,
            program_id=str(state["program_id"]),
        )
        report = await handler(state)
        notes = (*report.notes, *(f"{agent.value} drafted {draft}" for draft in report.drafts))
        return {"routed_to": (agent,), "notes": notes}

    return node


def supervisor_dispatcher(ports: PortBundle) -> ToolDispatcher:
    """The supervisor's own dispatcher, for callers that want one assessment.

    Exposed so a dashboard endpoint can read the same way the graph does — through
    the R3-gated toolset — instead of reaching past it to the ports directly.
    """
    return bind(toolset_for(AgentName.SUPERVISOR), ports)
