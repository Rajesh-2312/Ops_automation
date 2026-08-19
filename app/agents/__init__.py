"""The agent layer. CLAUDE.md §8, under R1, R2 and R3.

    "agents that draft and retrieve — never agents that decide or send"

WHERE THE RULES ARE ENFORCED, SO A READER CAN CHECK THEM RATHER THAN TRUST THEM
==============================================================================

R3 — agents have no release capability. Five independent mechanisms, strongest
first. Any one of them alone stops a send-capable tool being *called*; all five
have to be defeated for one to exist:

  1. `tools/catalog.py`  — a `ToolSpec` is pure data with no field that can hold
                           a callable, so nothing send-capable can be bound.
  2. `tools/dispatch.py` — a closed `match` table, no `getattr`, every arm
                           landing on a protocol method.
  3. `ports.py`          — that protocol surface has reads and one `save_draft`.
                           There is no method that sends.
  4. `tools/catalog.py`  — `ToolEffect` has two members and `describe_effect()`
                           ends in `assert_never`, so a third is a mypy error.
  5. `tools/rule_linter.py` (L3) — static check over the toolset declarations and
                           over every import under `app/agents/`.

R1 — the database owns truth, the LLM owns language. `runtime.AgentRuntime.
generate()` is the only way an agent obtains prose, and it runs
`grounding.assert_grounded` over the output before returning it. A figure the
structured input did not contain raises rather than shipping.

R2 — no money is computed by an LLM. Nothing here imports
`app.services.remuneration`; a test asserts it. Ranking scores (`sourcing.
rank_profiles`) and the whole of the supervisor's assessment are computed in pure
Python for the same reason, and the model explains them.

§8's autonomy ladder — `runtime.AGENT_CEILINGS` transcribes the table's Ceiling
column, and `require_ceiling()` refuses to construct an agent above it. Phase 4
(§13) ships Intake and Sourcing at level 2, Draft.

§11 — agent I/O is logged on every invocation: prompt size, tools called, tokens,
latency. Saving a draft writes an `AuditEvent` in the same transaction as the
draft itself.
"""

from __future__ import annotations

from app.agents.assessment import AssessmentAgent
from app.agents.grounding import UngroundedFigureError, assert_grounded
from app.agents.intake import IntakeAgent, IntakeResult
from app.agents.logistics import LogisticsAgent
from app.agents.monitor import MonitorAgent
from app.agents.onboarding import OnboardingAgent
from app.agents.payout import PayoutAgent
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
from app.agents.reporting import ReportingAgent
from app.agents.runtime import (
    AGENT_CEILINGS,
    AgentInvocation,
    AgentRuntime,
    AutonomyCeilingError,
    DraftOutcome,
    require_ceiling,
)
from app.agents.sourcing import ProfileRanking, SourcingAgent, SpecDiff, diff_spec, rank_profiles
from app.agents.supervisor import (
    ProgramAssessment,
    SupervisorState,
    assess,
    build_supervisor_graph,
)
from app.agents.tools import AGENT_TOOLSETS, AgentName, AgentToolset, PortBundle, bind, toolset_for

__all__ = [
    "AGENT_CEILINGS",
    "AGENT_TOOLSETS",
    "AgentInvocation",
    "AgentName",
    "AgentRuntime",
    "AgentToolset",
    "AssessmentAgent",
    "AutonomyCeilingError",
    "ContactSnapshot",
    "DocumentSnapshot",
    "Draft",
    "DraftOutcome",
    "DraftSink",
    "IntakeAgent",
    "IntakeResult",
    "LogisticsAgent",
    "MonitorAgent",
    "OnboardingAgent",
    "PayoutAgent",
    "PortBundle",
    "ProfileRanking",
    "ProgramAssessment",
    "ProgramReadPort",
    "ProgramSnapshot",
    "ReportingAgent",
    "RetrievalPort",
    "RetrievedPassage",
    "SavedDraft",
    "SourcingAgent",
    "SourcingReadPort",
    "SpecDiff",
    "SupervisorState",
    "TaskSnapshot",
    "TrainerProfileSnapshot",
    "UngroundedFigureError",
    "assert_grounded",
    "assess",
    "bind",
    "build_supervisor_graph",
    "diff_spec",
    "rank_profiles",
    "require_ceiling",
    "toolset_for",
]
