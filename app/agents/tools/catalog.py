"""Agent tool declarations. CLAUDE.md R3 is this module's entire specification.

    "Agent tool sets contain read and `save_draft` only. There is no
     `send_email`, `send_whatsapp`, `post_message`, or `mark_released` tool bound
     to any agent graph. Release endpoints require an authenticated human
     session. This is enforced by tool binding, not by prompt instruction —
     never add a send-capable tool to an agent's toolset 'temporarily'."

HOW R3 IS STRUCTURAL HERE, NOT ASPIRATIONAL
===========================================
"Enforced by tool binding" is a demanding phrase. A list of tool names with a
test that greps for "send" is not enforcement; it is a spell check, and the first
tool called `notify_ta` or `dispatch_pack` walks straight through it. Four
properties do the actual work, and each one is independently sufficient to stop a
send-capable tool being *called*:

1. **A toolset holds no code.** `ToolSpec` is pure data — a name, an effect, a
   description, an argument shape. It has no `func`, no `coroutine`, no
   `callable` field, and no place to put one. You therefore cannot bind a sending
   function to a tool, because a tool is not a thing that holds a function. This
   is the property that makes the rest more than naming discipline.

2. **Dispatch is a closed table over a protocol with no send method.** What a
   tool name resolves to is decided in `app.agents.tools.dispatch` by an
   exhaustive `match` over this catalogue, and each arm calls a method on a
   `Protocol` in `app.agents.ports`. Read that protocol surface: reads, and one
   `save_draft`. There is no method that could send, so there is no target a
   tool could be pointed at that would send.

3. **The effect vocabulary is closed, and closing it is checked by the type
   checker.** `ToolEffect` has two members. `describe_effect()` below matches
   exhaustively and ends in `typing.assert_never`, so adding a third member —
   `SEND`, say — does not produce a working new capability. It produces a mypy
   error in this file, before any test runs. That is the difference between a
   convention and a constraint.

4. **The registry is total.** `AGENT_TOOLSETS` maps every member of `AgentName`,
   and `tests/unit/test_agents_toolsets.py` asserts that the mapping's keys equal
   the enum's members. A new agent cannot be added without appearing in the R3
   assertion, so the test cannot be outrun by growth. `AgentToolset` is frozen
   and exposes its tools as an immutable tuple, so nothing can be appended to one
   at runtime either — an addition is a source edit, reviewed, in this file.

On top of those, `tools/rule_linter.py` (rule L3) reads the per-agent name tuples
below statically and rejects any name that is neither a read prefix nor
`save_draft`, and rejects send-suggestive imports anywhere under `app/agents/`.
That linter is the outermost and weakest of the five checks. It is listed last
deliberately: if it were the only one, R3 would be aspirational.

WHY `ToolEffect` LIVES HERE AND NOT IN `app/domain/enums.py`
============================================================
§11 puts enums in `domain/`, and the general rule is right. This one follows the
precedent already set by `app.core.audit.AuditAction` and
`app.services.approval.state_machine.ApprovalAction`: it is not a status stored
in a column and mirrored by a Postgres enum, it is a vocabulary owned by exactly
one module. Keeping it beside the exhaustive `match` that closes it is what makes
property 3 above work — a reader who adds a member sees the `assert_never` in the
same file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, assert_never

__all__ = [
    "AGENT_TOOLSETS",
    "READ_AND_DRAFT_TOOLS",
    "SAVE_DRAFT",
    "AgentName",
    "AgentToolset",
    "ToolEffect",
    "ToolSpec",
    "UnknownToolError",
    "describe_effect",
    "toolset_for",
]


class AgentName(StrEnum):
    """The supervisor and the nine specialists of CLAUDE.md §8.

    All ten are named even though Phase 4 implements two of them (Intake and
    Sourcing Liaison, per §13). That is deliberate: `AGENT_TOOLSETS` must be total
    over this enum, so declaring the whole roster now means the R3 test covers
    every agent from the day its name exists rather than from the day somebody
    remembers to add it to the test.
    """

    SUPERVISOR = "supervisor"
    INTAKE = "intake"
    SOURCING = "sourcing"
    ONBOARDING = "onboarding"
    LOGISTICS = "logistics"
    MONITOR = "monitor"
    ASSESSMENT = "assessment"
    REPORTING = "reporting"
    PAYOUT = "payout"
    COPILOT = "copilot"


class ToolEffect(StrEnum):
    """What a tool does to the world. Two members, and that is the point.

    R3 permits exactly two capabilities. Modelling them as a closed enum, matched
    exhaustively in `describe_effect()`, means the type checker participates in
    enforcing the rule: a third member is a compile-time failure in this file, not
    a new feature.

    There is no `WRITE`. `SAVE_DRAFT` is deliberately narrower than "write" —
    it names the one artifact state an agent may create (R4's DRAFT), and
    `app.agents.ports.SavedDraft` refuses to represent any other.
    """

    READ = "read"
    SAVE_DRAFT = "save_draft"


def describe_effect(effect: ToolEffect) -> str:
    """One line explaining an effect, for the approval UI and for logs.

    The `assert_never` is load-bearing, not decoration. It is what turns "R3 says
    two capabilities" into something mypy checks: add `ToolEffect.SEND` and this
    function stops type checking, because `effect` is no longer `Never` at the
    final branch. Do not replace it with an `else: return "unknown"` — that would
    quietly readmit the third capability the rule forbids.
    """
    match effect:
        case ToolEffect.READ:
            return "reads a system of record; changes nothing"
        case ToolEffect.SAVE_DRAFT:
            return "saves a DRAFT for a human to review, edit and send (autonomy level 2)"
    assert_never(effect)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool an agent may call. Pure data — this type holds no code.

    THE MISSING FIELD IS THE FEATURE
    --------------------------------
    There is no `func`, `coroutine`, `handler` or `callable` attribute, and one
    must never be added. A `ToolSpec` names a capability; `app.agents.tools.
    dispatch` decides what that name executes, against the closed protocol
    surface in `app.agents.ports`. Splitting declaration from binding is what
    makes "no send-capable tool is bound" a property of the code rather than a
    claim about it: there is no field in which a send-capable function could be
    parked.

    `args` describes the parameters by name and human-readable type, for the
    model's tool description. It is deliberately not a Pydantic model — §11 wants
    Pydantic at API boundaries, and this is an internal declaration whose only
    consumer is a prompt string and a log line.
    """

    name: str
    effect: ToolEffect
    description: str
    args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ToolSpec.name must be non-empty")
        if not self.description.strip():
            # The description is what the model reads to decide whether to call
            # the tool. An undescribed tool gets called at random or not at all.
            raise ValueError(f"ToolSpec {self.name!r} needs a description")

    def render(self) -> str:
        """The tool as one line of prompt text, effect included.

        The effect is stated to the model as well as enforced in code. Belt and
        braces, in that order — R3 is explicit that the enforcement is the tool
        binding and the prompt text is merely courtesy to the model.
        """
        signature = ", ".join(self.args)
        return f"{self.name}({signature}) -> {self.description} [{describe_effect(self.effect)}]"


# --- the catalogue ----------------------------------------------------------
#
# Every tool that exists. Named `*_TOOLS` so that rule_linter's L3 rule matches
# the binding and checks each entry statically (read_/get_/list_/search_ prefix,
# or exactly `save_draft`). Keeping the whole catalogue in one tuple means the
# linter sees every tool in the platform in one place.

#: The single write capability. Bound by name from `SAVE_DRAFT` so no agent
#: toolset has to spell the string, and so this constant is the one grep handle
#: for "which agents can write anything at all".
SAVE_DRAFT: Final[str] = "save_draft"

READ_AND_DRAFT_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_program",
        effect=ToolEffect.READ,
        description="Read one program: college, stage, type, start and end dates",
        args=("program_id",),
    ),
    ToolSpec(
        name="list_program_tasks",
        effect=ToolEffect.READ,
        description="List every task on a program with status, stage, due date and blockers",
        args=("program_id",),
    ),
    ToolSpec(
        name="list_program_documents",
        effect=ToolEffect.READ,
        description="List a program's document obligations and whether each is signed",
        args=("program_id",),
    ),
    ToolSpec(
        name="list_internal_contacts",
        effect=ToolEffect.READ,
        description="List people associated with a program, each carrying their persona",
        args=("program_id",),
    ),
    ToolSpec(
        name="list_candidate_profiles",
        effect=ToolEffect.READ,
        description="List trainer profiles TA has submitted against a program's requirement",
        args=("program_id",),
    ),
    ToolSpec(
        name="read_requirement_spec",
        effect=ToolEffect.READ,
        description="Read the current requirement spec for a program, for the re-spec diff",
        args=("program_id",),
    ),
    ToolSpec(
        name="search_corpus",
        effect=ToolEffect.READ,
        description=(
            "Search a permissioned RAG corpus for cited policy passages "
            "(policy and context only, never figures — CLAUDE.md §9)"
        ),
        args=("corpus", "query", "limit"),
    ),
    ToolSpec(
        name=SAVE_DRAFT,
        effect=ToolEffect.SAVE_DRAFT,
        description=(
            "Save a DRAFT artifact and its audit row for a human to review, edit and send. "
            "Does not submit, approve or release"
        ),
        args=("draft",),
    ),
)

_BY_NAME: Final[Mapping[str, ToolSpec]] = MappingProxyType(
    {spec.name: spec for spec in READ_AND_DRAFT_TOOLS}
)


class UnknownToolError(KeyError):
    """A toolset named a tool that is not in `READ_AND_DRAFT_TOOLS`.

    Raised at import time, because a toolset is built at module scope. An agent
    whose toolset names a tool nobody declared must not start — that is precisely
    how an undeclared capability would arrive, and failing at import makes it
    impossible to ship.
    """


@dataclass(frozen=True, slots=True)
class AgentToolset:
    """The closed, inspectable set of tools one agent may call.

    Frozen, and `tools` is a tuple: there is no `add()`, no `extend()`, no
    `__setitem__`. Granting an agent a capability is an edit to the source below,
    which a reviewer sees and the linter reads. Nothing can widen a toolset at
    runtime, which is the shape "temporarily" usually takes.

    Constructed from tool *names* rather than `ToolSpec` objects so that the
    declaration a human reads (and the one rule_linter L3 parses) is a flat tuple
    of strings, and so an agent cannot smuggle in a locally-defined spec that was
    never added to the catalogue.
    """

    agent: AgentName
    tools: tuple[ToolSpec, ...]

    @classmethod
    def of(cls, agent: AgentName, names: tuple[str, ...]) -> AgentToolset:
        """Resolve names against the catalogue. Raises on anything unknown."""
        specs: list[ToolSpec] = []
        for name in names:
            spec = _BY_NAME.get(name)
            if spec is None:
                raise UnknownToolError(
                    f"{agent.value} names tool {name!r}, which is not in "
                    "READ_AND_DRAFT_TOOLS. Every tool an agent may call is declared in "
                    "app/agents/tools/catalog.py — CLAUDE.md R3."
                )
            specs.append(spec)
        return cls(agent=agent, tools=tuple(specs))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.tools)

    @property
    def effects(self) -> frozenset[ToolEffect]:
        """The distinct capabilities this agent holds. A test asserts the set."""
        return frozenset(spec.effect for spec in self.tools)

    @property
    def can_write(self) -> bool:
        """True when the agent may save a draft. It is never true for more."""
        return ToolEffect.SAVE_DRAFT in self.effects

    def render(self) -> str:
        """The whole toolset as prompt text."""
        return "\n".join(spec.render() for spec in self.tools)


# --- per-agent bindings (CLAUDE.md §8, "Owns" column) -----------------------
#
# Each tuple below is what rule_linter L3 reads. Read tools carry a read_/list_/
# search_/get_ prefix and the one write is exactly `save_draft`; anything else in
# these tuples fails the lint before it fails a test.

#: The supervisor reads program state and routes. It drafts nothing itself and it
#: contacts nobody: §8 says it "never contacts an external party", and the way
#: that is guaranteed is that it holds no write tool at all, not even save_draft.
#: Its output is a routing decision and an internal nudge list, both returned as
#: graph state for a human dashboard to render.
SUPERVISOR_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_tasks",
    "list_program_documents",
    "list_internal_contacts",
)

#: Intake: "Parse MoU/PO/mail -> structured Program draft, flag unusual clauses".
#: Reads the contracts corpus for the clause norms it flags against; writes one
#: draft. Ceiling: Draft.
INTAKE_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_documents",
    "search_corpus",
    "save_draft",
)

#: Sourcing Liaison: "Requirement spec, TA follow-up, re-spec diffs, profile
#: ranking". The follow-up is a DRAFT chase message a human sends — there is no
#: send tool here and there must never be one. Ceiling: Draft.
SOURCING_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_tasks",
    "list_candidate_profiles",
    "read_requirement_spec",
    "list_internal_contacts",
    "search_corpus",
    "save_draft",
)

#: Onboarding: WO / ZOHO / ERM / platform-access checklist, internal chase.
#: §8 puts its ceiling at "Auto (internal only)", which is autonomy level 4 — but
#: R3 is absolute and unconditional, so even at level 4 this agent holds no send
#: tool. It drafts into the Comms Service queue (§8, "shared services, not
#: agents"), and any auto-release of an internal chase is a policy on that
#: queue, executed by a service under a human-set rule, never by the agent. See
#: `app.agents.runtime.AGENT_CEILINGS` for the same note.
ONBOARDING_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_tasks",
    "list_program_documents",
    "list_internal_contacts",
    "save_draft",
)

#: Logistics: travel need detection, booking request, onward and return.
LOGISTICS_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_tasks",
    "list_internal_contacts",
    "save_draft",
)

#: Delivery Monitor: attendance, usage, syllabus anomalies, risk scoring.
#: Ceiling "Alert (internal only)" — level 1, Observe. No write tool: an alert is
#: a read plus a log line, and the Escalation Engine (deterministic, not an
#: agent) owns what happens next.
MONITOR_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_tasks",
    "list_internal_contacts",
)

#: Assessment: request assembly, Tech-team chase, report package.
ASSESSMENT_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_tasks",
    "list_internal_contacts",
    "search_corpus",
    "save_draft",
)

#: Reporting: governance report, feedback synthesis, college summaries.
REPORTING_TOOLS: tuple[str, ...] = (
    "read_program",
    "list_program_tasks",
    "list_program_documents",
    "search_corpus",
    "save_draft",
)

#: Payout: "Explain validation failures, draft variance reasons, run summaries".
#: Note what is absent: any tool that returns a computed amount. R2 — an agent may
#: explain a number, never produce one — and the numbers it explains are passed in
#: as structured input by the caller that ran the engine, not fetched by the agent.
PAYOUT_TOOLS: tuple[str, ...] = (
    "read_program",
    "search_corpus",
    "save_draft",
)

#: Ops Copilot: RAG Q&A. Read-only, level 1, and it cannot even draft.
COPILOT_TOOLS: tuple[str, ...] = (
    "search_corpus",
    "read_program",
)


#: Total over `AgentName`. The test asserts `set(AGENT_TOOLSETS) == set(AgentName)`,
#: so an agent cannot exist without its capabilities being visible to the R3
#: assertion. Immutable at runtime for the same reason `AgentToolset` is frozen.
AGENT_TOOLSETS: Final[Mapping[AgentName, AgentToolset]] = MappingProxyType(
    {
        AgentName.SUPERVISOR: AgentToolset.of(AgentName.SUPERVISOR, SUPERVISOR_TOOLS),
        AgentName.INTAKE: AgentToolset.of(AgentName.INTAKE, INTAKE_TOOLS),
        AgentName.SOURCING: AgentToolset.of(AgentName.SOURCING, SOURCING_TOOLS),
        AgentName.ONBOARDING: AgentToolset.of(AgentName.ONBOARDING, ONBOARDING_TOOLS),
        AgentName.LOGISTICS: AgentToolset.of(AgentName.LOGISTICS, LOGISTICS_TOOLS),
        AgentName.MONITOR: AgentToolset.of(AgentName.MONITOR, MONITOR_TOOLS),
        AgentName.ASSESSMENT: AgentToolset.of(AgentName.ASSESSMENT, ASSESSMENT_TOOLS),
        AgentName.REPORTING: AgentToolset.of(AgentName.REPORTING, REPORTING_TOOLS),
        AgentName.PAYOUT: AgentToolset.of(AgentName.PAYOUT, PAYOUT_TOOLS),
        AgentName.COPILOT: AgentToolset.of(AgentName.COPILOT, COPILOT_TOOLS),
    }
)


def toolset_for(agent: AgentName) -> AgentToolset:
    """The toolset bound to one agent. Total over `AgentName` by construction."""
    return AGENT_TOOLSETS[agent]
