"""The Onboarding agent. CLAUDE.md §8: "WO / ZOHO / ERM / platform-access
checklist, internal chase". Ceiling: Auto (internal only).

The assertions divide the way the module does. The checklist is a pure function
over tracker rows and is tested as one — deterministically, with no model in
sight. The chase is prose, tested with a mocked LLM, and what it asserts is R1:
the model may *explain* the checklist and may not invent a count in it.

The file's centre of gravity is the ceiling. This is the only specialist above
Draft, so it is the one where somebody could reason "level 4, therefore it may
send". Several assertions below exist to make that reasoning fail loudly: the
toolset's effects, the agent's construction-time refusal of a widened toolset,
and the recipient filter that keeps an external party out of the prompt entirely.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from app.agents.grounding import UngroundedFigureError, collect_grounded_values, figures_in
from app.agents.onboarding import (
    OnboardingAgent,
    OnboardingChecklist,
    OnboardingStep,
    StepState,
    build_checklist,
)
from app.agents.ports import DocumentSnapshot
from app.agents.runtime import AgentRuntime
from app.agents.tools import AgentName, PortBundle, ToolEffect, bind, toolset_for
from app.agents.tools.catalog import AgentToolset
from app.domain.enums import (
    ArtifactState,
    AutonomyLevel,
    DocStatus,
    LLMTask,
    Persona,
    ProgramStage,
    TaskStatus,
)
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeDraftSink,
    FakeLLM,
    FakeProgramPort,
    a_contact,
    a_program,
    a_task,
)


def a_document(
    title: str, *, signed: bool = False, status: DocStatus = DocStatus.SENT
) -> DocumentSnapshot:
    return DocumentSnapshot(document_id=uuid4(), title=title, status=status.value, signed=signed)


def an_onboarding_task(title, **kwargs):
    return a_task(title, stage=ProgramStage.TRAINER_ONBOARDING, **kwargs)


def checklist_of(*, documents: tuple = (), tasks: tuple = ()) -> OnboardingChecklist:
    return build_checklist(PROGRAM_ID, documents, tasks)


def state_of(checklist: OnboardingChecklist, step: OnboardingStep) -> StepState:
    return next(item.state for item in checklist.items if item.step is step)


# --- the checklist: pure Python, no LLM (R1) ---------------------------------


def test_every_step_appears_on_every_checklist() -> None:
    """Total over `OnboardingStep`, in declaration order. The row an operator
    learns to look for is always in the same place."""
    checklist = checklist_of()
    assert tuple(item.step for item in checklist.items) == tuple(OnboardingStep)


def test_a_step_nobody_created_a_task_for_is_not_tracked_not_done() -> None:
    """The finding this agent is worth having. A checklist that omitted the step
    nobody opened would read as complete, which is how a trainer reaches a campus
    with no platform access."""
    checklist = checklist_of(documents=(a_document("Work order", signed=True),))
    assert state_of(checklist, OnboardingStep.WORK_ORDER) is StepState.DONE
    assert state_of(checklist, OnboardingStep.ZOHO) is StepState.NOT_TRACKED
    assert state_of(checklist, OnboardingStep.ERM) is StepState.NOT_TRACKED
    assert state_of(checklist, OnboardingStep.PLATFORM_ACCESS) is StepState.NOT_TRACKED
    assert not checklist.is_complete


def test_a_checklist_is_complete_only_when_all_four_steps_are_done() -> None:
    checklist = checklist_of(
        documents=(a_document("Signed work order", signed=True, status=DocStatus.SIGNED),),
        tasks=(
            an_onboarding_task("Create ZOHO account", status=TaskStatus.DONE),
            an_onboarding_task("ERM sync", status=TaskStatus.DONE),
            an_onboarding_task("Grant platform access", status=TaskStatus.DONE),
        ),
    )
    assert checklist.is_complete
    assert checklist.outstanding == ()


def test_an_unsigned_work_order_is_outstanding_however_its_status_reads() -> None:
    """`signed` is the fact; `status` is a label. Onboarding turns on the fact."""
    checklist = checklist_of(documents=(a_document("Work order", signed=False),))
    assert state_of(checklist, OnboardingStep.WORK_ORDER) is StepState.OUTSTANDING


def test_blocked_beats_outstanding() -> None:
    """Unblocking is the actionable half — the precedence `supervisor.assess` uses."""
    checklist = checklist_of(
        tasks=(
            an_onboarding_task("ERM sync", status=TaskStatus.BLOCKED),
            an_onboarding_task("ERM field pack", status=TaskStatus.PENDING),
        )
    )
    assert state_of(checklist, OnboardingStep.ERM) is StepState.BLOCKED
    assert len(checklist.blocked) == 1


def test_an_unmet_dependency_blocks_even_when_the_tracker_says_pending() -> None:
    """The dependency graph is the truth; the status column is a cache of it."""
    predecessor = uuid4()
    checklist = checklist_of(
        tasks=(
            a_task("Sign work order", task_id=predecessor, status=TaskStatus.PENDING),
            an_onboarding_task("Create ZOHO account", blocked_by=(predecessor,)),
        )
    )
    assert state_of(checklist, OnboardingStep.ZOHO) is StepState.BLOCKED


def test_a_finished_task_is_not_blocked_by_history() -> None:
    """A DONE task whose predecessor is still open is finished, not blocked."""
    predecessor = uuid4()
    checklist = checklist_of(
        tasks=(
            a_task("Sign work order", task_id=predecessor),
            an_onboarding_task(
                "Create ZOHO account", blocked_by=(predecessor,), status=TaskStatus.DONE
            ),
        )
    )
    assert state_of(checklist, OnboardingStep.ZOHO) is StepState.DONE


def test_steps_are_matched_across_every_stage_not_just_onboarding() -> None:
    """A work order obligation is one whatever stage the tracker filed it under."""
    checklist = checklist_of(
        tasks=(a_task("Issue work order", stage=ProgramStage.ACQUISITION_SETUP),)
    )
    assert state_of(checklist, OnboardingStep.WORK_ORDER) is StepState.OUTSTANDING


def test_one_task_can_be_evidence_for_two_steps() -> None:
    """Dropping it from one would hide an obligation; showing it twice is untidy."""
    checklist = checklist_of(tasks=(an_onboarding_task("Push the work order into ERM"),))
    assert state_of(checklist, OnboardingStep.WORK_ORDER) is StepState.OUTSTANDING
    assert state_of(checklist, OnboardingStep.ERM) is StepState.OUTSTANDING


def test_matching_is_not_fuzzy_so_a_stray_title_is_surfaced_not_attached() -> None:
    """`Workshop` must not match `work order`. An unmatched onboarding task is
    reported for a human to classify rather than force-fitted."""
    task = an_onboarding_task("Workshop logistics walkthrough")
    checklist = checklist_of(tasks=(task,))
    assert all(item.state is StepState.NOT_TRACKED for item in checklist.items)
    assert checklist.unclassified_tasks == (task,)


def test_erm_does_not_match_the_middle_of_another_word() -> None:
    """`erm` is a substring of "terms", "determine" and "permission". A checklist
    that reported ERM as under way because somebody opened a task about contract
    terms would never get fixed, because it does not look broken."""
    for title in ("Confirm the terms with the college", "Determine trainer permissions"):
        checklist = checklist_of(tasks=(an_onboarding_task(title),))
        assert state_of(checklist, OnboardingStep.ERM) is StepState.NOT_TRACKED


def test_unclassified_is_scoped_to_onboarding_stage_tasks() -> None:
    """A program-wide "matched nothing" list would be every task on the program."""
    checklist = checklist_of(tasks=(a_task("Collect feedback", stage=ProgramStage.DEPLOYMENT),))
    assert checklist.unclassified_tasks == ()


def test_only_internal_owners_are_carried_on_an_item() -> None:
    """§4: a trainer is a record, not a user. It is never an owner to chase."""
    checklist = checklist_of(
        tasks=(
            an_onboarding_task("Create ZOHO account", owner=a_contact(Persona.MANAGER, "R. M.")),
            an_onboarding_task("ERM sync", owner=a_contact(Persona.TRAINER, "VEMA PRUDHVI SAI")),
        )
    )
    zoho = next(item for item in checklist.items if item.step is OnboardingStep.ZOHO)
    erm = next(item for item in checklist.items if item.step is OnboardingStep.ERM)
    assert [owner.display_name for owner in zoho.owners] == ["R. M."]
    assert erm.owners == ()


def test_a_finished_task_contributes_no_owner_to_chase() -> None:
    checklist = checklist_of(
        tasks=(
            an_onboarding_task(
                "Create ZOHO account",
                status=TaskStatus.DONE,
                owner=a_contact(Persona.MANAGER, "R. M."),
            ),
        )
    )
    zoho = next(item for item in checklist.items if item.step is OnboardingStep.ZOHO)
    assert zoho.owners == ()


def test_the_checklist_is_deterministic() -> None:
    """Two runs over the same rows agree. No model, no clock, no set ordering."""
    tasks = (an_onboarding_task("ERM sync"), an_onboarding_task("Grant platform access"))
    assert checklist_of(tasks=tasks).as_payload() == checklist_of(tasks=tasks).as_payload()


# --- the agent ---------------------------------------------------------------


def build_agent(
    llm: FakeLLM,
    *,
    documents: tuple = (),
    tasks: tuple = (),
    contacts: tuple = (),
    autonomy: AutonomyLevel = AutonomyLevel.ACT,
) -> tuple[OnboardingAgent, FakeDraftSink]:
    sink = FakeDraftSink()
    ports = PortBundle(
        programs=FakeProgramPort(
            program=a_program(ProgramStage.TRAINER_ONBOARDING),
            documents=documents,
            tasks=tasks,
            contacts=contacts,
        ),
        drafts=sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.ONBOARDING,
        dispatcher=bind(toolset_for(AgentName.ONBOARDING), ports),
        llm=llm,
        autonomy=autonomy,
    )
    return OnboardingAgent(runtime=runtime), sink


async def test_the_checklist_path_calls_no_model_at_all() -> None:
    """R1: what is signed and what is done are database facts. Level 4 acting on
    internal state needs no language, so no model is called and none can be."""
    llm = FakeLLM(responses=[])
    agent, sink = build_agent(llm, tasks=(an_onboarding_task("ERM sync"),))
    checklist = await agent.assess_onboarding(PROGRAM_ID)
    assert state_of(checklist, OnboardingStep.ERM) is StepState.OUTSTANDING
    assert llm.calls == []
    assert sink.saved == []


async def test_a_chase_is_drafted_and_never_sent() -> None:
    """R3/R4: level 4 buys no send tool. The chase lands in DRAFT."""
    agent, sink = build_agent(
        FakeLLM(responses=["ZOHO and ERM are still open. Please close them this week."]),
        tasks=(an_onboarding_task("Create ZOHO account"), an_onboarding_task("ERM sync")),
        contacts=(a_contact(Persona.MANAGER),),
    )
    outcome = await agent.draft_internal_chase(PROGRAM_ID)
    assert outcome.saved.state is ArtifactState.DRAFT
    assert len(sink.saved) == 1


async def test_the_chase_routes_to_the_volume_tier() -> None:
    """§2: chase is volume work, routed by task and not by default."""
    llm = FakeLLM(responses=["ZOHO is still open."])
    agent, _ = build_agent(
        llm,
        tasks=(an_onboarding_task("Create ZOHO account"),),
        contacts=(a_contact(Persona.MANAGER),),
    )
    outcome = await agent.draft_internal_chase(PROGRAM_ID)
    assert llm.tasks_called == [LLMTask.CHASE]
    assert outcome.invocation.model == "fake/volume"


async def test_a_chase_is_addressed_only_to_internal_colleagues() -> None:
    """§4/§8: an external party is filtered out before the model is called, so it
    is not even in the prompt. This is the agent whose ceiling could auto-release."""
    llm = FakeLLM(responses=["ZOHO is still open."])
    agent, sink = build_agent(
        llm,
        tasks=(an_onboarding_task("Create ZOHO account"),),
        contacts=(
            a_contact(Persona.MANAGER, "R. Maroju"),
            a_contact(Persona.TRAINER, "VEMA PRUDHVI SAI"),
            a_contact(Persona.COLLEGE, "Malineni Principal"),
        ),
    )
    await agent.draft_internal_chase(PROGRAM_ID)

    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == ["R. Maroju"]
    assert draft.payload["audience"] == "internal_staff"
    prompt = str(llm.calls[0]["user"])
    assert "VEMA PRUDHVI SAI" not in prompt
    assert "Malineni Principal" not in prompt


async def test_a_chase_with_no_internal_recipient_is_flagged_not_readdressed() -> None:
    """It does not fall back to the college contact. It asks a human."""
    agent, sink = build_agent(
        FakeLLM(responses=["ZOHO is still open."]),
        tasks=(an_onboarding_task("Create ZOHO account"),),
        contacts=(a_contact(Persona.COLLEGE, "Malineni Principal"),),
    )
    await agent.draft_internal_chase(PROGRAM_ID)
    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == []
    assert "no internal recipient identified — assign before sending" in draft.flags


async def test_untracked_steps_are_flagged_for_creation_not_for_hurrying() -> None:
    agent, sink = build_agent(
        FakeLLM(responses=["The work order is open; nothing tracks the other steps."]),
        documents=(a_document("Work order"),),
        contacts=(a_contact(Persona.MANAGER),),
    )
    await agent.draft_internal_chase(PROGRAM_ID)
    draft, _ = sink.saved[0]
    assert "no task or document tracks the zoho step — open one" in draft.flags
    assert "no task or document tracks the erm step — open one" in draft.flags
    assert "no task or document tracks the platform_access step — open one" in draft.flags


async def test_a_complete_checklist_says_so_rather_than_chasing() -> None:
    agent, sink = build_agent(
        FakeLLM(responses=["Onboarding is complete."]),
        documents=(a_document("Work order", signed=True, status=DocStatus.SIGNED),),
        tasks=(
            an_onboarding_task("Create ZOHO account", status=TaskStatus.DONE),
            an_onboarding_task("ERM sync", status=TaskStatus.DONE),
            an_onboarding_task("Grant platform access", status=TaskStatus.DONE),
        ),
        contacts=(a_contact(Persona.MANAGER),),
    )
    await agent.draft_internal_chase(PROGRAM_ID)
    draft, _ = sink.saved[0]
    assert "every onboarding step is complete — no chase is needed" in draft.flags


# --- R1 / §12: no figure the checklist did not contain ------------------------


async def test_a_count_the_model_invented_is_refused() -> None:
    """§12: "compare every number in generated text against the structured input".
    Three outstanding steps is a fact; five is a fabrication, and the draft is
    refused rather than corrected."""
    agent, sink = build_agent(
        FakeLLM(responses=["There are 5 onboarding steps outstanding on this program."]),
        tasks=(an_onboarding_task("Create ZOHO account"),),
        contacts=(a_contact(Persona.MANAGER),),
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_internal_chase(PROGRAM_ID)
    assert exc.value.context == "onboarding.internal_chase"
    assert not sink.saved


async def test_an_invented_due_date_is_refused() -> None:
    """A date is a factual claim R1 governs as much as an amount is."""
    agent, sink = build_agent(
        FakeLLM(responses=["ZOHO must be created by 2026-09-30."]),
        tasks=(an_onboarding_task("Create ZOHO account", due_on=dt.date(2026, 8, 25)),),
        contacts=(a_contact(Persona.MANAGER),),
    )
    with pytest.raises(UngroundedFigureError):
        await agent.draft_internal_chase(PROGRAM_ID)
    assert not sink.saved


async def test_every_figure_in_the_drafted_body_is_in_the_structured_input() -> None:
    """§12's assertion made directly, rather than trusting the runtime's check."""
    agent, sink = build_agent(
        FakeLLM(responses=["1 step is outstanding; ZOHO is due on 2026-08-25."]),
        tasks=(an_onboarding_task("Create ZOHO account", due_on=dt.date(2026, 8, 25)),),
        contacts=(a_contact(Persona.MANAGER),),
    )
    await agent.draft_internal_chase(PROGRAM_ID)
    draft, _ = sink.saved[0]
    allowed = collect_grounded_values(draft.grounded_in)
    assert figures_in(draft.body), "the assertion is worthless if the body has no figures"
    for written, value in figures_in(draft.body):
        assert value in allowed, f"{written!r} is not in the structured input"


# --- R3: the ceiling grants nothing ------------------------------------------


def test_the_onboarding_toolset_holds_no_release_capable_tool() -> None:
    """§12: "assert no agent toolset exposes a release-capable tool". Asserted on
    the effects, not on the names — the strong form."""
    toolset = toolset_for(AgentName.ONBOARDING)
    assert toolset.effects <= {ToolEffect.READ, ToolEffect.SAVE_DRAFT}
    for forbidden in ("send_email", "send_whatsapp", "post_message", "mark_released"):
        assert forbidden not in toolset.names


def test_onboarding_is_level_four_and_still_holds_only_read_and_save_draft() -> None:
    """The whole point of the "Auto (internal only)" note: a high ceiling is not a
    capability grant. R3 is unconditional."""
    from app.agents.runtime import AGENT_CEILINGS

    assert AGENT_CEILINGS[AgentName.ONBOARDING] is AutonomyLevel.ACT
    assert {spec.effect for spec in toolset_for(AgentName.ONBOARDING).tools} == {
        ToolEffect.READ,
        ToolEffect.SAVE_DRAFT,
    }


def test_the_agent_refuses_to_build_on_a_widened_toolset() -> None:
    """A toolset carrying an effect outside R3's two fails at construction, not at
    the first release attempt."""
    from app.agents.tools.catalog import ToolSpec

    widened = AgentToolset(
        agent=AgentName.ONBOARDING,
        tools=(
            *toolset_for(AgentName.ONBOARDING).tools,
            ToolSpec(
                name="read_program",
                effect="release",  # type: ignore[arg-type]
                description="a capability R3 does not permit",
            ),
        ),
    )
    runtime = AgentRuntime(
        agent=AgentName.ONBOARDING,
        dispatcher=bind(widened, PortBundle()),
        llm=FakeLLM(),
        autonomy=AutonomyLevel.ACT,
    )
    with pytest.raises(PermissionError, match="R3 is unconditional"):
        OnboardingAgent(runtime=runtime)


def test_the_agent_refuses_another_agents_runtime() -> None:
    runtime = AgentRuntime(
        agent=AgentName.SOURCING,
        dispatcher=bind(toolset_for(AgentName.SOURCING), PortBundle()),
        llm=FakeLLM(),
    )
    with pytest.raises(ValueError, match="onboarding runtime"):
        OnboardingAgent(runtime=runtime)


async def test_everything_the_chase_did_before_generating_was_a_read() -> None:
    """§11's "tools called" record, read back as an R3 assertion. Every tool this
    agent invoked to assemble the prompt reads; the only write is the draft."""
    agent, sink = build_agent(
        FakeLLM(responses=["ZOHO is still open."]),
        tasks=(an_onboarding_task("Create ZOHO account"),),
        contacts=(a_contact(Persona.MANAGER),),
    )
    outcome = await agent.draft_internal_chase(PROGRAM_ID)
    assert [call.tool for call in outcome.invocation.tools_called] == [
        "read_program",
        "list_program_documents",
        "list_program_tasks",
        "list_internal_contacts",
    ]
    assert all(call.effect is ToolEffect.READ for call in outcome.invocation.tools_called)
    assert len(sink.saved) == 1


async def test_the_audit_row_records_the_agent_and_the_model() -> None:
    """§11: every state transition writes an AuditEvent — actor, action, before, after."""
    agent, sink = build_agent(
        FakeLLM(responses=["ZOHO is still open."]),
        tasks=(an_onboarding_task("Create ZOHO account"),),
        contacts=(a_contact(Persona.MANAGER),),
    )
    actor = uuid4()
    await agent.draft_internal_chase(PROGRAM_ID, actor_id=actor, actor_persona=Persona.MANAGER)
    _, event = sink.saved[0]
    assert event.actor_id == actor
    assert event.action == "agent.draft_saved"
    assert event.before is None
    after = event.after or {}
    assert after["agent"] == "onboarding"
    assert after["model"] == "fake/volume"
    # The audit row records the level the write happened at, and an agent's only
    # write is a DRAFT write however high its ceiling.
    assert after["autonomy"] == AutonomyLevel.DRAFT.value


def test_a_checklist_item_is_immutable() -> None:
    """The evidence a state was derived from must not drift after derivation."""
    item = checklist_of(tasks=(an_onboarding_task("ERM sync"),)).items[0]
    with pytest.raises((AttributeError, TypeError)):
        item.state = StepState.DONE  # type: ignore[misc]
