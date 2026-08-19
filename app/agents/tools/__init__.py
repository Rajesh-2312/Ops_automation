"""Agent tool definitions — READ AND DRAFT ONLY (CLAUDE.md §3, R3).

Two modules, and the split between them is the point:

* `catalog` declares tools as **pure data**. A `ToolSpec` has no field that could
  hold a function, so a send-capable function cannot be bound to a tool.
* `dispatch` turns a name into a call through a **closed table** over the
  protocols in `app.agents.ports`, which expose reads and one `save_draft`.

Read the module docstring of `catalog` for the full argument, including why the
`ToolEffect` enum's exhaustive `match` makes a third capability a type error
rather than a feature.
"""

from __future__ import annotations

from app.agents.tools.catalog import (
    AGENT_TOOLSETS,
    READ_AND_DRAFT_TOOLS,
    SAVE_DRAFT,
    AgentName,
    AgentToolset,
    ToolEffect,
    ToolSpec,
    UnknownToolError,
    describe_effect,
    toolset_for,
)
from app.agents.tools.dispatch import (
    PortBundle,
    PortUnavailableError,
    ToolCall,
    ToolDispatcher,
    ToolNotBoundError,
    UnroutableToolError,
    bind,
)

__all__ = [
    "AGENT_TOOLSETS",
    "READ_AND_DRAFT_TOOLS",
    "SAVE_DRAFT",
    "AgentName",
    "AgentToolset",
    "PortBundle",
    "PortUnavailableError",
    "ToolCall",
    "ToolDispatcher",
    "ToolEffect",
    "ToolNotBoundError",
    "ToolSpec",
    "UnknownToolError",
    "UnroutableToolError",
    "bind",
    "describe_effect",
    "toolset_for",
]
