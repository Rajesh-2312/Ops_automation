"""What every agent shares: ceilings, the logged LLM call, and the draft write.

Three CLAUDE.md obligations live here so that nine specialists cannot implement
them nine slightly different ways.

**§8's autonomy ladder, as data.** `AGENT_CEILINGS` transcribes the "Ceiling"
column of the §8 table. `require_ceiling()` refuses to build an agent that claims
more autonomy than §8 grants it, and nothing in Phase 4 may exceed level 2
(Draft) — "propose, human edits and sends".

**§11's logging obligation.** "Log agent I/O — prompt, tools called, tokens,
latency — for every invocation." `AgentRuntime.generate()` emits all four on
every call, joining the LLM half to the tool half the dispatcher accumulated. It
is not optional and there is no quiet path around it: agents call `generate()`,
not the `LLMClient` directly.

**R1 at the point of generation.** `generate()` runs `assert_grounded` over the
model's output against the structured input it was given. An agent physically
cannot obtain prose it has not grounded, because the only method that returns
prose is the one that checks it. That is the same design move as the toolset
holding no code: make the unsafe thing unreachable rather than discouraged.

WHY THE LLM ARRIVES AS A PROTOCOL
=================================
`Completer` mirrors the one method of `app.core.llm.LLMClient` that agents use.
`LLMClient` is still the only implementation and OpenRouter remains the sole
gateway (§2) — the protocol exists so tests can supply a canned response without
an API key, not so a second gateway can be slipped in. Note what agents *cannot*
do through it: there is no `model` parameter anywhere in this file. Routing is by
`LLMTask` through `TASK_TIER`, so "route by task, not by default" (§2) holds and
no model id is ever hardcoded in an agent.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol
from uuid import UUID

import structlog
from pydantic import JsonValue

from app.agents.grounding import assert_grounded
from app.agents.ports import Draft, SavedDraft
from app.agents.tools.catalog import AgentName, ToolEffect
from app.agents.tools.dispatch import ToolCall, ToolDispatcher
from app.core.audit import AuditEvent
from app.core.llm import LLMResponse
from app.domain.enums import AutonomyLevel, LLMTask, Persona

__all__ = [
    "AGENT_CEILINGS",
    "AgentAction",
    "AgentInvocation",
    "AgentRuntime",
    "AutonomyCeilingError",
    "Completer",
    "DraftOutcome",
    "require_ceiling",
]

_log = structlog.get_logger(__name__)


#: CLAUDE.md §8, the "Ceiling" column, transcribed. Not inferred, not defaulted:
#: an agent absent from this mapping cannot be constructed.
#:
#: ONBOARDING is level 4 and MONITOR level 1 per the table's "Auto (internal
#: only)" and "Alert (internal only)". Neither holds a send tool, and neither may
#: ever hold one — R3 is unconditional and does not bend for a high ceiling. What
#: level 4 buys the Onboarding agent is that a draft it puts on the Comms Service
#: queue may be released by that service under a human-set policy without a
#: per-message click; the agent still only drafts. See the note on
#: `ONBOARDING_TOOLS` in `app.agents.tools.catalog`, and §14 — the exact policy
#: is not specified in CLAUDE.md and is not invented here.
AGENT_CEILINGS: Final[Mapping[AgentName, AutonomyLevel]] = {
    AgentName.SUPERVISOR: AutonomyLevel.OBSERVE,
    AgentName.INTAKE: AutonomyLevel.DRAFT,
    AgentName.SOURCING: AutonomyLevel.DRAFT,
    AgentName.ONBOARDING: AutonomyLevel.ACT,
    AgentName.LOGISTICS: AutonomyLevel.DRAFT,
    AgentName.MONITOR: AutonomyLevel.OBSERVE,
    AgentName.ASSESSMENT: AutonomyLevel.DRAFT,
    AgentName.REPORTING: AutonomyLevel.DRAFT,
    AgentName.PAYOUT: AutonomyLevel.DRAFT,
    AgentName.COPILOT: AutonomyLevel.OBSERVE,
}


class AgentAction:
    """Audit vocabulary for the agent layer.

    A class of constants rather than a `StrEnum` for the reason
    `app.core.audit.AuditAction` documents at length: `AuditEvent.action` is
    typed `str` precisely so a workstream can name its own actions without
    editing a shared file. These are the two an agent can cause.

    Note what is not here and never will be: no `agent.sent`, no
    `agent.released`. An agent has no action that could produce those rows (R3).
    """

    DRAFT_SAVED: Final[str] = "agent.draft_saved"
    ROUTED: Final[str] = "agent.routed"


class AutonomyCeilingError(RuntimeError):
    """An agent was asked to operate above the ceiling §8 gives it."""

    def __init__(self, agent: AgentName, requested: AutonomyLevel) -> None:
        ceiling = AGENT_CEILINGS[agent]
        super().__init__(
            f"Agent '{agent.value}' has ceiling {ceiling.name} (level {ceiling.value}) per "
            f"CLAUDE.md §8, but level {requested.value} ({requested.name}) was requested. "
            "Nothing touching money, contracts, or a college contact goes past level 3, and "
            "Phase 4 (§13) ships Intake and Sourcing at level 2 — Draft."
        )
        self.agent = agent
        self.requested = requested


def require_ceiling(agent: AgentName, requested: AutonomyLevel) -> None:
    """Raise unless `agent` may operate at `requested`. Returns `None` on success.

    Called at graph construction, not per invocation: a graph that could exceed
    its ceiling should fail to build, so the failure lands on the developer
    rather than on the first program that trips the path.
    """
    if requested > AGENT_CEILINGS[agent]:
        raise AutonomyCeilingError(agent, requested)


class Completer(Protocol):
    """The one `app.core.llm.LLMClient` method agents use. §2's sole gateway.

    No `model` parameter, deliberately — see the module docstring.
    """

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
class AgentInvocation:
    """The §11 record of one agent invocation: prompt, tools, tokens, latency.

    Frozen and returned to the caller alongside the work, rather than merely
    logged, so a test can assert the record exists and an operator dashboard can
    show what an agent cost without parsing log lines.

    `prompt_chars` rather than the prompt itself: a prompt carries the structured
    input, which for several agents is trainer PII (§4). The full text goes to the
    debug-level log in `app.core.llm`, behind the same gate everything else PII
    sits behind.
    """

    agent: AgentName
    task: LLMTask
    model: str
    prompt_chars: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    tools_called: tuple[ToolCall, ...] = ()
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    """A saved draft, the audit row that recorded it, and what it cost.

    The trio travels together for the reason `ApprovalOutcome` gives in
    `app.services.approval.state_machine`: returning the artifact without its
    audit event makes it possible to persist one and forget the other, and §11
    does not treat the audit row as optional.
    """

    draft: Draft
    saved: SavedDraft
    event: AuditEvent
    invocation: AgentInvocation


@dataclass
class AgentRuntime:
    """The shared machinery of one agent: its dispatcher, its LLM, its ceiling.

    Holds no tools of its own — everything it can do, it does through
    `dispatcher`, which is gated by the agent's toolset (R3).
    """

    agent: AgentName
    dispatcher: ToolDispatcher
    llm: Completer
    #: The level this instance operates at. Checked against §8 on construction.
    autonomy: AutonomyLevel = AutonomyLevel.DRAFT

    def __post_init__(self) -> None:
        require_ceiling(self.agent, self.autonomy)
        if self.dispatcher.toolset.agent is not self.agent:
            raise ValueError(
                f"AgentRuntime for '{self.agent.value}' was handed the toolset of "
                f"'{self.dispatcher.toolset.agent.value}'. Toolsets are per-agent bindings "
                "(CLAUDE.md R3) and must not be shared across agents."
            )
        if self.autonomy > AutonomyLevel.DRAFT and self.dispatcher.toolset.can_write:
            # Phase 4 (§13) ships draft agents. An agent above level 2 that can also
            # write is exactly the combination §8 says to promote "one agent at a
            # time, with rollback" in Phase 7 — not something to acquire by default.
            _log.warning(
                "agent.autonomy_above_draft_with_write",
                agent=self.agent.value,
                autonomy=self.autonomy.value,
            )

    async def generate(
        self,
        task: LLMTask,
        *,
        system: str,
        user: str,
        structured_input: object,
        context: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> tuple[str, AgentInvocation]:
        """One grounded, logged completion. The only way an agent gets prose.

        `structured_input` is what the text is allowed to quote figures from — R1.
        It is a required argument with no default and no `None` shortcut: an agent
        that has no structured input has nothing it is permitted to state
        numerically, and passing `{}` says so explicitly.

        Raises `UngroundedFigureError` if the model states a figure that did not
        arrive as structured input. The failure is not retried here; the caller
        decides, and for a draft agent the right answer is usually to surface the
        failure rather than to roll the dice again (see `app.agents.intake`).
        """
        response = await self.llm.complete(
            task,
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        invocation = AgentInvocation(
            agent=self.agent,
            task=task,
            model=response.model,
            prompt_chars=len(system) + len(user),
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
            tools_called=tuple(self.dispatcher.calls),
        )
        # §11: prompt (size), tools called, tokens, latency — every invocation.
        _log.info(
            "agent.invocation",
            agent=self.agent.value,
            task=task.value,
            model=response.model,
            prompt_chars=invocation.prompt_chars,
            prompt_tokens=invocation.prompt_tokens,
            completion_tokens=invocation.completion_tokens,
            latency_ms=invocation.latency_ms,
            tools_called=[call.tool for call in invocation.tools_called],
        )

        # R1, checked before the caller can do anything with the text.
        assert_grounded(response.text, structured_input, context)
        return response.text, invocation

    async def save_draft(
        self,
        draft: Draft,
        invocation: AgentInvocation,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Persist a draft with its audit row. The agent layer's only write.

        `actor_id` defaults to `None` — §11's audit contract allows a NULL actor
        "only for a scheduled job with no human behind it", and an agent run by
        the hourly supervisor is exactly that. When a human triggered the agent
        their id is passed through, so the trail says who asked.

        The `AuditEvent` is built here and handed to the sink, which persists both
        in one transaction (`app.agents.ports.DraftSink`).
        """
        if not self.dispatcher.toolset.can_write:
            # Unreachable through `dispatcher.save_draft`, which refuses first.
            # Kept as an explicit second refusal because this is the method a new
            # agent author calls, and the error should name the rule.
            raise PermissionError(
                f"Agent '{self.agent.value}' holds no {ToolEffect.SAVE_DRAFT.value} "
                "capability. CLAUDE.md R3 — capability is tool binding, not intent."
            )

        event = AuditEvent(
            actor_id=actor_id,
            actor_persona=actor_persona,
            action=AgentAction.DRAFT_SAVED,
            entity_table=draft.artifact_type.value,
            entity_id=draft.program_id,
            before=None,
            after=_draft_snapshot(draft, invocation),
        )
        saved = await self.dispatcher.save_draft(draft, event)
        return DraftOutcome(draft=draft, saved=saved, event=event, invocation=invocation)


def _draft_snapshot(draft: Draft, invocation: AgentInvocation) -> dict[str, JsonValue]:
    """The audit row's view of a new draft. §11: actor, action, before, after, at.

    `before` is `None` because a draft has no prior state — this is the moment the
    artifact begins to exist. The `after` records which agent and which model
    produced it, which is the question asked first when a draft turns out to be
    wrong, and the flags, which are what the reviewer is being asked to look at.

    The body is fingerprinted by length rather than copied. An audit row is not a
    content store, and a drafted college communication in a second table would be
    a copy of reviewable content outside the artifact's own access rules (§4).
    """
    return {
        "agent": invocation.agent.value,
        "model": invocation.model,
        "llm_task": invocation.task.value,
        "artifact_type": draft.artifact_type.value,
        "title": draft.title,
        "body_chars": len(draft.body),
        "flags": list(draft.flags),
        "tools_called": [call.tool for call in invocation.tools_called],
        "autonomy": AutonomyLevel.DRAFT.value,
    }
