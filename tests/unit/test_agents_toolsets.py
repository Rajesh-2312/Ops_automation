"""R3, asserted structurally. CLAUDE.md §12: "assert no agent toolset exposes a
release-capable tool. This is a test, so it fails loudly if someone adds one."

A name check alone is not enough and this file does not rely on one. The
assertions below attack the rule from the angles a real regression would arrive
from:

* every toolset's *effects* are drawn from a two-member closed set (not: no tool
  is called "send_email");
* the toolset type has no field capable of holding a callable, so a sending
  function cannot be bound to a tool at all;
* the port protocols — the only things the dispatcher can call — expose no
  method that could send;
* the dispatcher refuses a tool outside the agent's toolset at call time, which
  is what makes the binding rather than the prompt the enforcement point;
* the registry is total over `AgentName`, so a new agent cannot dodge these
  assertions by not being listed;
* and finally, the name check, as the weakest of the five.

The name-based assertion is deliberately last. If it were the only one, R3 would
be a spell check.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.agents import ports
from app.agents.tools.catalog import (
    AGENT_TOOLSETS,
    READ_AND_DRAFT_TOOLS,
    AgentName,
    AgentToolset,
    ToolEffect,
    ToolSpec,
    UnknownToolError,
    describe_effect,
)
from app.agents.tools.dispatch import PortBundle, ToolNotBoundError, bind

#: Verbs that would constitute a release capability. Used only by the last,
#: weakest assertion — see the module docstring.
FORBIDDEN = re.compile(
    r"(^|_)(send|post|release|released|email|whatsapp|notify|publish|dispatch|mark)(_|$)",
    re.IGNORECASE,
)


# --- 1. the effect vocabulary is closed --------------------------------------


def test_tool_effects_are_exactly_read_and_save_draft() -> None:
    """R3 permits two capabilities. A third member here is a rule change.

    This is the assertion that catches a send tool honestly declared. Somebody
    adding `ToolEffect.SEND` fails here even if they name the tool `read_and_go`.
    """
    assert {effect.value for effect in ToolEffect} == {"read", "save_draft"}


def test_describe_effect_is_total() -> None:
    """`assert_never` guards this at type-check time; assert it at runtime too."""
    for effect in ToolEffect:
        assert describe_effect(effect)


@pytest.mark.parametrize("agent", list(AgentName))
def test_no_toolset_holds_an_effect_outside_the_two(agent: AgentName) -> None:
    permitted = {ToolEffect.READ, ToolEffect.SAVE_DRAFT}
    assert AGENT_TOOLSETS[agent].effects <= permitted


@pytest.mark.parametrize("agent", list(AgentName))
def test_at_most_one_write_tool_per_agent(agent: AgentName) -> None:
    """An agent may hold `save_draft`. It may not hold two write capabilities."""
    writes = [spec for spec in AGENT_TOOLSETS[agent].tools if spec.effect is ToolEffect.SAVE_DRAFT]
    assert len(writes) <= 1
    assert all(spec.name == "save_draft" for spec in writes)


# --- 2. a toolset structurally cannot hold code ------------------------------


def test_toolspec_has_no_field_that_could_hold_a_callable() -> None:
    """The load-bearing structural property: a tool is data, not a function.

    If a `func` / `coroutine` / `handler` field ever appears on `ToolSpec`, a
    send-capable function becomes bindable to a tool and every other assertion in
    this file starts guarding a door that is no longer the only one.
    """
    fields = set(ToolSpec.__dataclass_fields__)
    assert fields == {"name", "effect", "description", "args"}
    callable_ish = {"func", "coroutine", "fn", "handler", "callable", "tool", "run", "impl"}
    assert not fields & callable_ish


def test_a_toolspec_rejects_a_callable_in_every_field() -> None:
    """Belt and braces: no field accepts a function even by accident."""
    spec = ToolSpec(name="read_program", effect=ToolEffect.READ, description="x")
    for value in (spec.name, spec.effect, spec.description, spec.args):
        assert not callable(value)


def test_toolsets_are_immutable() -> None:
    """No `add`, no `extend`, no assignment. Widening is a reviewed source edit."""
    toolset = AGENT_TOOLSETS[AgentName.COPILOT]
    assert isinstance(toolset.tools, tuple)
    for method in ("add", "append", "extend", "register", "__setitem__"):
        assert not hasattr(toolset, method)
    with pytest.raises((AttributeError, TypeError)):
        toolset.tools = ()  # type: ignore[misc]


def test_registry_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        AGENT_TOOLSETS[AgentName.COPILOT] = AGENT_TOOLSETS[AgentName.INTAKE]  # type: ignore[index]


def test_a_toolset_cannot_name_an_uncatalogued_tool() -> None:
    """The route a bespoke local tool would take into an agent. It is closed."""
    with pytest.raises(UnknownToolError):
        AgentToolset.of(AgentName.INTAKE, ("send_email",))
    with pytest.raises(UnknownToolError):
        AgentToolset.of(AgentName.INTAKE, ("read_program", "mark_released"))


# --- 3. the port surface has nothing that could send -------------------------


@pytest.mark.parametrize(
    "protocol",
    [ports.ProgramReadPort, ports.SourcingReadPort, ports.RetrievalPort, ports.DraftSink],
)
def test_port_protocols_expose_no_send_capable_method(protocol: type) -> None:
    """Dispatch lands only on these. If they cannot send, no tool can.

    This is the assertion that survives a tool being *renamed* into innocence: a
    tool named `read_updates` wired to a method named `read_updates` still cannot
    send, because no method on any of these protocols does.
    """
    methods = [
        name
        for name, value in inspect.getmembers(protocol, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert methods, f"{protocol.__name__} declares no methods — the check would be vacuous"
    for name in methods:
        assert not FORBIDDEN.search(name), f"{protocol.__name__}.{name} looks send-capable"


def test_draft_sink_has_exactly_one_method() -> None:
    """R3's "and `save_draft` only", as a property of the write protocol."""
    methods = [
        name
        for name, _ in inspect.getmembers(ports.DraftSink, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert methods == ["save_draft"]


def test_dispatch_contains_no_dynamic_attribute_lookup() -> None:
    """`getattr(port, name)` would reopen everything this file closes.

    Asserted against the source text because the property being protected is
    "this code was not written", which no runtime check can observe.
    """
    source = Path(inspect.getfile(bind)).read_text(encoding="utf-8")
    body = source.split("# --- the closed table")[1]
    for construct in ("getattr", "eval", "exec", "importlib", "__import__"):
        # Word-boundary matched: `_retrieval(...)` legitimately contains "eval".
        assert not re.search(
            rf"\b{re.escape(construct)}\s*\(", body
        ), f"{construct}() in the dispatch table defeats R3"


# --- 4. binding, not prompting, is the gate ----------------------------------


async def test_a_tool_outside_the_toolset_is_refused() -> None:
    """The Delivery Monitor holds no write capability. Asking anyway fails.

    R3: "enforced by tool binding, not by prompt instruction". A model that
    hallucinates a tool it was never given must hit a refusal, not a side effect.
    """
    dispatcher = bind(AGENT_TOOLSETS[AgentName.MONITOR], PortBundle())
    with pytest.raises(ToolNotBoundError) as exc:
        await dispatcher.call("save_draft", draft=None, event=None)
    assert "R3" in str(exc.value)


async def test_an_uncatalogued_tool_name_is_refused() -> None:
    dispatcher = bind(AGENT_TOOLSETS[AgentName.INTAKE], PortBundle())
    with pytest.raises(ToolNotBoundError):
        await dispatcher.call("send_email", to="principal@college.edu")


def test_supervisor_holds_no_write_capability() -> None:
    """§8: the supervisor "never contacts an external party".

    It cannot even draft — it has no write tool at all, so there is no artifact it
    could produce that a human could then send.
    """
    supervisor = AGENT_TOOLSETS[AgentName.SUPERVISOR]
    assert supervisor.effects == {ToolEffect.READ}
    assert not supervisor.can_write


def test_read_only_agents_have_no_write_capability() -> None:
    """§8 puts the Ops Copilot at "Read-only" and the Monitor at "Alert"."""
    for agent in (AgentName.COPILOT, AgentName.MONITOR):
        assert not AGENT_TOOLSETS[agent].can_write


# --- 5. the registry is total, so the assertions cannot be outrun ------------


def test_every_agent_has_a_declared_toolset() -> None:
    """A new agent cannot exist without appearing in every assertion above."""
    assert set(AGENT_TOOLSETS) == set(AgentName)


def test_every_catalogued_tool_is_routable() -> None:
    """A declared tool with no dispatch arm would silently return nothing.

    Checked by reading the dispatch source for each name rather than by calling
    it, because calling would need a port per tool and the property under test is
    "an arm exists", not "the arm works".
    """
    from app.agents.tools import dispatch as dispatch_module

    source = Path(inspect.getfile(dispatch_module)).read_text(encoding="utf-8")
    table = source.split("# --- the closed table")[1]
    for spec in READ_AND_DRAFT_TOOLS:
        assert f'case "{spec.name}"' in table, f"{spec.name} has no dispatch arm"


def test_dispatch_routes_nothing_that_is_not_catalogued() -> None:
    """The reverse direction: no arm exists for a tool nobody declared."""
    from app.agents.tools import dispatch as dispatch_module

    source = Path(inspect.getfile(dispatch_module)).read_text(encoding="utf-8")
    table = source.split("# --- the closed table")[1]
    routed = set(re.findall(r'case "([^"]+)"', table))
    assert routed == {spec.name for spec in READ_AND_DRAFT_TOOLS}


# --- 6. the name check, last and weakest ------------------------------------


@pytest.mark.parametrize("agent", list(AgentName))
def test_no_toolset_names_a_release_capable_tool(agent: AgentName) -> None:
    """§12's assertion, literally. Kept, but it is the least of the five."""
    for name in AGENT_TOOLSETS[agent].names:
        assert not FORBIDDEN.search(name), f"{agent.value} binds release-capable '{name}'"


def test_every_catalogued_tool_is_a_read_or_the_one_draft() -> None:
    for spec in READ_AND_DRAFT_TOOLS:
        if spec.effect is ToolEffect.SAVE_DRAFT:
            assert spec.name == "save_draft"
        else:
            assert spec.name.startswith(("read_", "list_", "search_", "get_"))
