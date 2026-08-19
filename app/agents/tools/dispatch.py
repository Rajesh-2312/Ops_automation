"""Turning a tool name into a call. The second lock on CLAUDE.md R3.

`app.agents.tools.catalog` guarantees a toolset holds no code. This module is
where a name from that catalogue becomes an actual call, and it is deliberately
the only such place. Two properties matter:

**The dispatch table is closed.** `_invoke` is one `match` over the catalogue's
names with a final arm that raises. There is no registry an unrelated module can
append to, no entry-point discovery, no `getattr(port, name)` — that last one is
the tempting shortcut and it is exactly what would let a tool called
`read_program` reach a method called `read_program` on an object that also has a
`send()`. Every arm is written out, and a test asserts the table covers the
catalogue exactly, so a declared tool that nobody routed fails loudly instead of
silently doing nothing.

**Every arm lands on `app.agents.ports`.** Those protocols expose reads and one
`save_draft`. The dispatcher cannot call anything else because nothing else is in
scope: it holds a `PortBundle`, not a service locator.

**A tool not in the agent's toolset is refused at call time.** The toolset is not
merely advertised to the model in the prompt; it is checked here. A model that
hallucinates `save_draft` while running as the Delivery Monitor — which holds no
write capability (§8, "Alert (internal only)") — gets a `ToolNotBoundError`, not
a draft. R3's "enforced by tool binding, not by prompt instruction" is precisely
this check: the prompt is advice, this is the gate.

§11 requires agent I/O to be logged, "prompt, tools called, tokens, latency". The
"tools called" half is recorded here, on the `ToolLog` the dispatcher accumulates,
and `app.agents.runtime` joins it to the LLM half.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from pydantic import JsonValue

from app.agents.ports import (
    ContactSnapshot,
    DocumentSnapshot,
    Draft,
    DraftSink,
    ProgramReadPort,
    ProgramSnapshot,
    RetrievalPort,
    RetrievedPassage,
    SavedDraft,
    SourcingReadPort,
    TaskSnapshot,
    TrainerProfileSnapshot,
)
from app.agents.tools.catalog import SAVE_DRAFT, AgentToolset, ToolEffect
from app.core.audit import AuditEvent

__all__ = [
    "PortBundle",
    "PortUnavailableError",
    "ToolCall",
    "ToolDispatcher",
    "ToolNotBoundError",
    "UnroutableToolError",
    "bind",
]

_log = structlog.get_logger(__name__)


class ToolNotBoundError(RuntimeError):
    """The agent asked for a tool that is not in its toolset.

    A `RuntimeError` and not a `ValueError`: this is a capability refusal, and a
    broad `except ValueError` around argument parsing must never be able to
    swallow "this agent tried to write something it may not write" (the same
    reasoning `app.services.approval.state_machine.ApprovalError` gives).
    """


class UnroutableToolError(RuntimeError):
    """A catalogued tool that `_invoke` has no arm for.

    Only reachable if somebody adds a `ToolSpec` and forgets the dispatch arm.
    `tests/unit/test_agents_toolsets.py` asserts the table is total over the
    catalogue, so this should fail in CI rather than in production — but it
    raises rather than returning `None`, because a tool that silently returns
    nothing is a fact-free answer, and R1 makes fact-free answers the failure
    mode that matters.
    """


class PortUnavailableError(RuntimeError):
    """A tool was called but the port that serves it was not supplied.

    Ports are optional on `PortBundle` so a graph can be built with only what it
    needs — the Copilot needs no `DraftSink`, and handing it one would give it a
    write capability its §8 ceiling ("Read-only") does not include. Asking for a
    missing port is a wiring bug and is reported as one.
    """


@dataclass(frozen=True, slots=True)
class PortBundle:
    """The ports available to one graph. All optional, none of them send.

    Supply the narrowest bundle an agent needs. Omitting `drafts` is a real,
    enforceable way to make an agent read-only at the wiring level in addition to
    the toolset level — belt and braces on R3's one write.
    """

    programs: ProgramReadPort | None = None
    sourcing: SourcingReadPort | None = None
    retrieval: RetrievalPort | None = None
    drafts: DraftSink | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation, for the §11 log and the approval UI.

    `result_summary` is a shape description ("7 tasks"), never the payload. Tool
    results routinely contain trainer PII and, for some ports, commercials (§4);
    a log line is the wrong place for either.
    """

    tool: str
    effect: ToolEffect
    latency_ms: int
    result_summary: str


@dataclass
class ToolDispatcher:
    """Executes catalogued tools for one agent, against one port bundle.

    Not frozen, because it accumulates `calls` — the §11 "tools called" record.
    Everything that decides *what may be called* (the toolset) is frozen; only the
    transcript grows.
    """

    toolset: AgentToolset
    ports: PortBundle
    calls: list[ToolCall] = field(default_factory=list)

    async def call(self, tool: str, **kwargs: Any) -> Any:  # noqa: ANN401
        """Run one tool. Refuses anything outside this agent's toolset.

        `Any` in and `Any` out is honest here: the return type genuinely varies
        per tool, and the typed surface callers should use is the specific helper
        methods below, not this. The gate is the point of this method, not the
        signature.
        """
        spec = next((s for s in self.toolset.tools if s.name == tool), None)
        if spec is None:
            raise ToolNotBoundError(
                f"Agent '{self.toolset.agent.value}' called tool {tool!r}, which is not in "
                f"its toolset ({', '.join(self.toolset.names)}). CLAUDE.md R3: agent "
                "capability is enforced by tool binding, not by prompt instruction."
            )

        started = time.perf_counter()
        result = await _invoke(tool, self.ports, kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)

        record = ToolCall(
            tool=tool,
            effect=spec.effect,
            latency_ms=latency_ms,
            result_summary=_summarise(result),
        )
        self.calls.append(record)
        _log.info(
            "agent.tool_call",
            agent=self.toolset.agent.value,
            tool=tool,
            effect=spec.effect.value,
            latency_ms=latency_ms,
            result=record.result_summary,
        )
        return result

    # --- typed helpers -------------------------------------------------------
    #
    # Callers use these rather than `call()` so the agent modules stay typed. Each
    # goes through `call()`, so the toolset gate and the §11 log apply either way.

    async def read_program(self, program_id: UUID) -> ProgramSnapshot | None:
        result: ProgramSnapshot | None = await self.call("read_program", program_id=program_id)
        return result

    async def list_program_tasks(self, program_id: UUID) -> Sequence[TaskSnapshot]:
        result: Sequence[TaskSnapshot] = await self.call(
            "list_program_tasks", program_id=program_id
        )
        return result

    async def list_program_documents(self, program_id: UUID) -> Sequence[DocumentSnapshot]:
        result: Sequence[DocumentSnapshot] = await self.call(
            "list_program_documents", program_id=program_id
        )
        return result

    async def list_internal_contacts(self, program_id: UUID) -> Sequence[ContactSnapshot]:
        result: Sequence[ContactSnapshot] = await self.call(
            "list_internal_contacts", program_id=program_id
        )
        return result

    async def list_candidate_profiles(self, program_id: UUID) -> Sequence[TrainerProfileSnapshot]:
        result: Sequence[TrainerProfileSnapshot] = await self.call(
            "list_candidate_profiles", program_id=program_id
        )
        return result

    async def read_requirement_spec(self, program_id: UUID) -> Mapping[str, JsonValue] | None:
        result: Mapping[str, JsonValue] | None = await self.call(
            "read_requirement_spec", program_id=program_id
        )
        return result

    async def search_corpus(
        self, corpus: str, query: str, limit: int = 5
    ) -> Sequence[RetrievedPassage]:
        result: Sequence[RetrievedPassage] = await self.call(
            "search_corpus", corpus=corpus, query=query, limit=limit
        )
        return result

    async def save_draft(self, draft: Draft, event: AuditEvent) -> SavedDraft:
        """The one write. Returns a `SavedDraft`, which can only be DRAFT.

        Note the shape of the guarantee: even if a `DraftSink` implementation
        tried to approve or release what it was given, `SavedDraft.__post_init__`
        refuses to represent the result, so the agent cannot observe or act on a
        non-draft outcome (R3, R4).
        """
        result: SavedDraft = await self.call(SAVE_DRAFT, draft=draft, event=event)
        return result


def bind(toolset: AgentToolset, ports: PortBundle) -> ToolDispatcher:
    """Bind a toolset to ports. The whole of "tool binding" in R3's sense.

    Deliberately a plain function with two arguments and no defaults: there is no
    ambient registry, no global dispatcher and no way to bind a tool that is not
    already in the catalogue and in the toolset.
    """
    return ToolDispatcher(toolset=toolset, ports=ports)


# --- the closed table -------------------------------------------------------


async def _invoke(tool: str, ports: PortBundle, kwargs: Mapping[str, Any]) -> Any:  # noqa: ANN401
    """Route one catalogued tool name to one port method. Closed by construction.

    Every arm names its port method literally. There is no `getattr` here and
    there must never be one: dynamic attribute lookup is how a name in a data
    file becomes an arbitrary method call, and the entire argument that R3 is
    structural rests on this table being written out and reviewable.
    """
    match tool:
        case "read_program":
            return await _programs(ports).read_program(kwargs["program_id"])
        case "list_program_tasks":
            return await _programs(ports).list_program_tasks(kwargs["program_id"])
        case "list_program_documents":
            return await _programs(ports).list_program_documents(kwargs["program_id"])
        case "list_internal_contacts":
            return await _programs(ports).list_internal_contacts(kwargs["program_id"])
        case "list_candidate_profiles":
            return await _sourcing(ports).list_candidate_profiles(kwargs["program_id"])
        case "read_requirement_spec":
            return await _sourcing(ports).read_requirement_spec(kwargs["program_id"])
        case "search_corpus":
            return await _retrieval(ports).search_corpus(
                kwargs["corpus"], kwargs["query"], kwargs.get("limit", 5)
            )
        case "save_draft":
            return await _drafts(ports).save_draft(kwargs["draft"], kwargs["event"])
    raise UnroutableToolError(
        f"Tool {tool!r} is declared in READ_AND_DRAFT_TOOLS but has no dispatch arm in "
        "app/agents/tools/dispatch.py. Add the arm; do not add a getattr fallback."
    )


def _programs(ports: PortBundle) -> ProgramReadPort:
    if ports.programs is None:
        raise PortUnavailableError("This graph was built without a ProgramReadPort")
    return ports.programs


def _sourcing(ports: PortBundle) -> SourcingReadPort:
    if ports.sourcing is None:
        raise PortUnavailableError("This graph was built without a SourcingReadPort")
    return ports.sourcing


def _retrieval(ports: PortBundle) -> RetrievalPort:
    if ports.retrieval is None:
        raise PortUnavailableError("This graph was built without a RetrievalPort")
    return ports.retrieval


def _drafts(ports: PortBundle) -> DraftSink:
    if ports.drafts is None:
        raise PortUnavailableError(
            "This graph was built without a DraftSink, so it cannot save a draft. "
            "That is a legitimate configuration for a read-only agent (CLAUDE.md §8 "
            "puts the Ops Copilot and the Delivery Monitor at autonomy level 1)."
        )
    return ports.drafts


def _summarise(result: object) -> str:
    """A shape description of a tool result. Never the contents — see `ToolCall`."""
    if result is None:
        return "none"
    if isinstance(result, SavedDraft):
        return f"draft {result.artifact_id} v{result.version} {result.state.value}"
    if isinstance(result, str):
        return f"str[{len(result)}]"
    if isinstance(result, Mapping):
        return f"mapping[{len(result)} keys]"
    if isinstance(result, Sequence):
        return f"{len(result)} item(s)"
    return type(result).__name__
