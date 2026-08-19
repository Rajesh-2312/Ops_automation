"""The Assessment agent. CLAUDE.md §8: "Assessment request assembly, Tech-team
chase, report package". Ceiling: Draft.

The assertions divide the way the module does. Completeness and gap counting are
pure functions and are tested as set operations and arithmetic, with no model in
sight. The prose paths are tested with a mocked LLM, and what they assert is that
the model may *explain* what was counted and may not restate, recount or fill in
a field the record does not hold (R1, and R2's shape: an agent may explain a
number, it may never produce one).

The sharpest assertion in the file is `test_an_absent_report_link_is_never_called
_a_missing_report`. An empty `report_url` means nobody pasted a link; it does not
mean the Tech team failed to produce a report. Stating the second would be an
agent asserting a fact it did not read from a system of record.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from app.agents.assessment import (
    REQUIRED_REQUEST_FIELDS,
    AssessmentAgent,
    AssessmentReportItem,
    AssessmentRequestSpec,
    missing_fields,
    package_gaps,
)
from app.agents.grounding import UngroundedFigureError
from app.agents.ports import RetrievedPassage
from app.agents.runtime import AgentRuntime, AutonomyCeilingError
from app.agents.tools import AgentName, PortBundle, ToolNotBoundError, bind, toolset_for
from app.domain.enums import (
    ArtifactState,
    ArtifactType,
    AutonomyLevel,
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
    FakeRetrievalPort,
    a_contact,
    a_program,
    a_task,
)


def a_spec(**overrides: object) -> AssessmentRequestSpec:
    fields: dict[str, object] = {
        "batch_label": "MLEC-CSE-B3",
        "syllabus_scope": "Arrays, linked lists, recursion",
        "scheduled_on": dt.date(2026, 7, 24),
        "student_count": 62,
        "duration_minutes": 90,
        "delivery_mode": "on campus",
    }
    fields.update(overrides)
    return AssessmentRequestSpec(**fields)  # type: ignore[arg-type]


def an_item(
    title: str, *, link: str | None = None, on: dt.date | None = None
) -> AssessmentReportItem:
    return AssessmentReportItem(title=title, report_url=link, conducted_on=on)


# --- request completeness: a set operation, no LLM (R1) ----------------------


def test_a_complete_spec_reports_nothing_missing() -> None:
    assert missing_fields(a_spec()) == ()


def test_an_empty_spec_reports_exactly_the_required_fields() -> None:
    """Pins the constant against the function, so the two cannot drift apart."""
    bare = AssessmentRequestSpec(batch_label="MLEC-CSE-B3")
    assert missing_fields(bare) == REQUIRED_REQUEST_FIELDS


def test_missing_fields_are_reported_in_a_fixed_order() -> None:
    """Two runs over one spec must produce the same list, or a reviewer reading
    two drafts sees two shapes for one problem."""
    spec = a_spec(syllabus_scope=None, student_count=None)
    assert missing_fields(spec) == ("syllabus_scope", "student_count")
    assert missing_fields(spec) == missing_fields(spec)


def test_a_whitespace_only_field_counts_as_missing() -> None:
    """A scope of " " is a field somebody tabbed past, not a decision."""
    assert missing_fields(a_spec(syllabus_scope="   ")) == ("syllabus_scope",)


def test_the_platform_field_is_not_required() -> None:
    """§14 Q6 is open. Flagging it on every request would train reviewers to
    ignore the flags that matter."""
    assert "platform" not in REQUIRED_REQUEST_FIELDS
    assert missing_fields(a_spec(platform=None)) == ()


def test_a_spec_refuses_an_undeclared_field() -> None:
    """extra="forbid": a field nobody declared cannot reach the prompt, where it
    would become quotable without ever having been reviewed."""
    with pytest.raises(ValueError):
        AssessmentRequestSpec(batch_label="B3", marks_out_of=100)  # type: ignore[call-arg]


def test_a_spec_is_frozen() -> None:
    with pytest.raises(ValueError):
        a_spec().batch_label = "other"  # type: ignore[misc]


# --- package gaps: counting, no LLM ------------------------------------------


def test_gaps_count_what_has_a_link_and_name_what_does_not() -> None:
    gaps = package_gaps(
        [
            an_item("Mid-term", link="https://clickup/report/1", on=dt.date(2026, 7, 10)),
            an_item("Unit 2", on=dt.date(2026, 7, 17)),
        ]
    )
    assert gaps.total == 2
    assert gaps.with_report_link == 1
    assert gaps.without_report_link == ("Unit 2",)
    assert not gaps.is_complete


def test_a_blank_link_is_treated_as_no_link() -> None:
    assert package_gaps([an_item("Unit 2", link="   ")]).without_report_link == ("Unit 2",)


def test_an_undated_assessment_is_named() -> None:
    gaps = package_gaps([an_item("Unit 2", link="https://x")])
    assert gaps.undated == ("Unit 2",)


def test_an_empty_package_is_not_complete() -> None:
    """ "0 of 0 have links" reads as healthy to anyone skimming."""
    gaps = package_gaps([])
    assert gaps.total == 0
    assert not gaps.is_complete


def test_a_full_package_is_complete() -> None:
    gaps = package_gaps([an_item("Unit 2", link="https://x", on=dt.date(2026, 7, 17))])
    assert gaps.is_complete


# --- the agent ---------------------------------------------------------------


def build_agent(
    llm: FakeLLM,
    *,
    contacts: tuple = (),
    tasks: tuple = (),
    passages: tuple = (),
) -> tuple[AssessmentAgent, FakeDraftSink]:
    sink = FakeDraftSink()
    ports = PortBundle(
        programs=FakeProgramPort(
            program=a_program(ProgramStage.ACTIVE_MONITORING),
            contacts=contacts,
            tasks=tasks,
        ),
        retrieval=FakeRetrievalPort(passages=passages),
        drafts=sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.ASSESSMENT,
        dispatcher=bind(toolset_for(AgentName.ASSESSMENT), ports),
        llm=llm,
    )
    return AssessmentAgent(runtime=runtime), sink


# --- 1. request assembly ------------------------------------------------------


async def test_a_request_is_drafted_not_issued() -> None:
    """R3/R4: it lands in DRAFT for a human to edit and send."""
    agent, sink = build_agent(
        FakeLLM(responses=["62 learners, 90 minutes, on campus, on 2026-07-24."])
    )
    outcome = await agent.draft_assessment_request(PROGRAM_ID, a_spec())
    assert outcome.saved.state is ArtifactState.DRAFT
    assert outcome.draft.artifact_type is ArtifactType.PROGRAM_DOCUMENT
    assert len(sink.saved) == 1


async def test_absent_fields_are_computed_before_the_model_sees_them() -> None:
    """The draft says "to be confirmed" about a field calculated to be absent,
    not one the model happened to notice."""
    llm = FakeLLM(responses=["Batch MLEC-CSE-B3; learner count to be confirmed."])
    agent, sink = build_agent(llm)
    await agent.draft_assessment_request(PROGRAM_ID, a_spec(student_count=None))

    draft, _ = sink.saved[0]
    assert draft.flags == ("not stated in the record: student_count",)
    assert "not_stated_in_record" in str(llm.calls[0]["user"])


async def test_a_count_the_model_invented_is_refused() -> None:
    """§12: compare every number in generated text against the structured input.
    Nothing may fill in a learner count the record does not hold."""
    agent, sink = build_agent(FakeLLM(responses=["Roughly 60 learners will sit this."]))
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_assessment_request(PROGRAM_ID, a_spec(student_count=None))
    assert exc.value.context == "assessment.request"
    assert not sink.saved


async def test_policy_is_retrieved_with_its_citation() -> None:
    """§9: no citation, no answer — and policy supplies rules, never figures."""
    agent, sink = build_agent(
        FakeLLM(responses=["Requested per the assessment SOP."]),
        passages=(
            RetrievedPassage(
                document_title="Assessment SOP",
                section="3.2 Notice",
                text="Requests are raised in advance of the assessment date.",
            ),
        ),
    )
    await agent.draft_assessment_request(PROGRAM_ID, a_spec())
    draft, _ = sink.saved[0]
    procedure = draft.grounded_in["procedure"]
    assert isinstance(procedure, list)
    assert procedure[0]["source"] == "Assessment SOP § 3.2 Notice"  # type: ignore[index]


async def test_the_request_routes_to_the_volume_tier() -> None:
    """§2: drafting is volume work, not frontier."""
    llm = FakeLLM(responses=["Batch MLEC-CSE-B3."])
    agent, _ = build_agent(llm)
    await agent.draft_assessment_request(PROGRAM_ID, a_spec())
    assert llm.tasks_called == [LLMTask.DRAFTING]


# --- 2. the Tech-team chase --------------------------------------------------


async def test_the_chase_is_addressed_only_to_internal_colleagues() -> None:
    """§4/§8: an external party is filtered out before the model is called."""
    llm = FakeLLM(responses=["Outstanding for 9 days."])
    agent, sink = build_agent(
        llm,
        contacts=(
            a_contact(Persona.MANAGER, "R. Maroju"),
            a_contact(Persona.TRAINER, "VEMA PRUDHVI SAI"),
            a_contact(Persona.COLLEGE, "Malineni Principal"),
        ),
    )
    await agent.draft_tech_team_chase(
        PROGRAM_ID,
        a_spec(),
        requested_on=dt.date(2026, 7, 11),
        as_of=dt.date(2026, 7, 20),
    )
    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == ["R. Maroju"]
    prompt = str(llm.calls[0]["user"])
    assert "VEMA PRUDHVI SAI" not in prompt
    assert "Malineni Principal" not in prompt


async def test_a_chase_with_no_internal_recipient_is_flagged_not_readdressed() -> None:
    """It does not fall back to the college contact. It asks a human."""
    agent, sink = build_agent(
        FakeLLM(responses=["The request is still open."]),
        contacts=(a_contact(Persona.COLLEGE, "Malineni Principal"),),
    )
    await agent.draft_tech_team_chase(
        PROGRAM_ID, a_spec(), requested_on=dt.date(2026, 7, 11), as_of=dt.date(2026, 7, 20)
    )
    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == []
    assert draft.flags == ("no internal recipient identified — assign before sending",)


async def test_days_outstanding_is_subtracted_in_python_and_quoted_by_the_model() -> None:
    """R2's shape outside money: the agent explains a number it was handed."""
    agent, sink = build_agent(
        FakeLLM(responses=["This has been open for 9 days."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    await agent.draft_tech_team_chase(
        PROGRAM_ID, a_spec(), requested_on=dt.date(2026, 7, 11), as_of=dt.date(2026, 7, 20)
    )
    draft, _ = sink.saved[0]
    assert draft.payload["days_outstanding"] == 9
    assert draft.grounded_in["days_outstanding"] == 9


async def test_a_different_elapsed_figure_is_refused() -> None:
    """9 days were computed; "14 days" was not handed to the model."""
    agent, sink = build_agent(
        FakeLLM(responses=["This has been open for 14 days."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_tech_team_chase(
            PROGRAM_ID, a_spec(), requested_on=dt.date(2026, 7, 11), as_of=dt.date(2026, 7, 20)
        )
    assert exc.value.context == "assessment.chase"
    assert not sink.saved


async def test_a_clock_that_runs_backwards_is_refused() -> None:
    """Nothing has been outstanding for a negative number of days."""
    agent, _ = build_agent(FakeLLM(responses=["never generated"]))
    with pytest.raises(ValueError, match="precedes requested_on"):
        await agent.draft_tech_team_chase(
            PROGRAM_ID, a_spec(), requested_on=dt.date(2026, 7, 20), as_of=dt.date(2026, 7, 11)
        )


async def test_only_open_monitoring_tasks_reach_the_chase_prompt() -> None:
    llm = FakeLLM(responses=["Open for 9 days."])
    agent, sink = build_agent(
        llm,
        contacts=(a_contact(Persona.MANAGER),),
        tasks=(
            a_task("Raise assessment request", stage=ProgramStage.ACTIVE_MONITORING),
            a_task("Collect signed MoU", stage=ProgramStage.ACQUISITION_SETUP),
            a_task(
                "Share results",
                stage=ProgramStage.ACTIVE_MONITORING,
                status=TaskStatus.DONE,
            ),
        ),
    )
    await agent.draft_tech_team_chase(
        PROGRAM_ID, a_spec(), requested_on=dt.date(2026, 7, 11), as_of=dt.date(2026, 7, 20)
    )
    draft, _ = sink.saved[0]
    tasks = draft.grounded_in["open_assessment_tasks"]
    assert isinstance(tasks, list)
    assert [task["title"] for task in tasks] == ["Raise assessment request"]  # type: ignore[index]


async def test_the_chase_routes_to_the_volume_tier() -> None:
    """§2: chase is volume work."""
    llm = FakeLLM(responses=["Open for 9 days."])
    agent, _ = build_agent(llm, contacts=(a_contact(Persona.MANAGER),))
    await agent.draft_tech_team_chase(
        PROGRAM_ID, a_spec(), requested_on=dt.date(2026, 7, 11), as_of=dt.date(2026, 7, 20)
    )
    assert llm.tasks_called == [LLMTask.CHASE]


async def test_a_chase_is_a_draft_and_is_never_sent() -> None:
    """R3: this agent has no send tool, so a chase can only ever be a draft."""
    agent, sink = build_agent(
        FakeLLM(responses=["Open for 9 days."]), contacts=(a_contact(Persona.MANAGER),)
    )
    outcome = await agent.draft_tech_team_chase(
        PROGRAM_ID, a_spec(), requested_on=dt.date(2026, 7, 11), as_of=dt.date(2026, 7, 20)
    )
    assert outcome.saved.state is ArtifactState.DRAFT
    # The §11 invocation record is taken when the completion returns, so it holds the
    # reads. The write follows it, and it is the one write the toolset permits.
    assert [call.tool for call in outcome.invocation.tools_called] == [
        "read_program",
        "list_program_tasks",
        "list_internal_contacts",
    ]
    assert [call.tool for call in agent.runtime.dispatcher.calls][-1] == "save_draft"
    assert len(sink.saved) == 1


# --- 3. the report package ---------------------------------------------------


async def test_the_package_counts_are_computed_before_the_model_sees_them() -> None:
    agent, sink = build_agent(FakeLLM(responses=["1 of 2 assessments has a report link."]))
    await agent.draft_report_package(
        PROGRAM_ID,
        [
            an_item("Mid-term", link="https://x", on=dt.date(2026, 7, 10)),
            an_item("Unit 2", on=dt.date(2026, 7, 17)),
        ],
    )
    draft, _ = sink.saved[0]
    assert draft.payload["package"] == {
        "total": 2,
        "with_report_link": 1,
        "without_report_link_count": 1,
        "without_report_link": ["Unit 2"],
        "undated": [],
    }


async def test_an_absent_report_link_is_never_called_a_missing_report() -> None:
    """The distinction this whole path exists to protect: an empty `report_url`
    means no link is recorded here, not that the Tech team produced nothing. The
    prompt says so in those words, and the flag does too."""
    llm = FakeLLM(responses=["Unit 2 has no report link recorded in this system."])
    agent, sink = build_agent(llm)
    await agent.draft_report_package(PROGRAM_ID, [an_item("Unit 2", on=dt.date(2026, 7, 17))])

    system = str(llm.calls[0]["system"])
    assert "no report link recorded" in system
    assert "Do not say the report is missing" in system
    draft, _ = sink.saved[0]
    assert draft.flags[0].startswith("1 assessment(s) have no report link recorded")


async def test_a_count_the_model_inflated_is_refused() -> None:
    agent, sink = build_agent(FakeLLM(responses=["3 of 5 assessments have reports."]))
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_report_package(
            PROGRAM_ID, [an_item("Unit 2", link="https://x", on=dt.date(2026, 7, 17))]
        )
    assert exc.value.context == "assessment.package"
    assert not sink.saved


async def test_an_empty_package_is_flagged_rather_than_narrated_as_healthy() -> None:
    agent, sink = build_agent(FakeLLM(responses=["No assessments were supplied."]))
    await agent.draft_report_package(PROGRAM_ID, [])
    draft, _ = sink.saved[0]
    assert draft.flags == ("no assessments supplied for this package",)


async def test_a_complete_package_carries_the_open_governance_question() -> None:
    """§14 Q3 — approval authority for college-facing comms — is unanswered, so
    the draft says so instead of picking the permissive option."""
    agent, sink = build_agent(FakeLLM(responses=["1 assessment, link on file."]))
    await agent.draft_report_package(
        PROGRAM_ID, [an_item("Unit 2", link="https://x", on=dt.date(2026, 7, 17))]
    )
    draft, _ = sink.saved[0]
    assert any("§14 Q3" in flag for flag in draft.flags)
    assert draft.artifact_type is ArtifactType.PROGRAM_DOCUMENT


async def test_the_package_routes_to_the_volume_tier() -> None:
    """§2: a summary is volume work."""
    llm = FakeLLM(responses=["1 assessment."])
    agent, _ = build_agent(llm)
    await agent.draft_report_package(
        PROGRAM_ID, [an_item("Unit 2", link="https://x", on=dt.date(2026, 7, 17))]
    )
    assert llm.tasks_called == [LLMTask.SUMMARY]


# --- R3, and the ceiling -----------------------------------------------------


def test_the_assessment_toolset_holds_no_release_capable_tool() -> None:
    """§12, literally: assert no agent toolset exposes a release-capable tool."""
    toolset = toolset_for(AgentName.ASSESSMENT)
    for forbidden in ("send_email", "send_whatsapp", "post_message", "mark_released"):
        assert forbidden not in toolset.names
    assert set(toolset.names) <= {
        "read_program",
        "list_program_tasks",
        "list_internal_contacts",
        "search_corpus",
        "save_draft",
    }
    assert toolset.can_write  # exactly one write, and it is the draft


async def test_a_tool_outside_the_toolset_is_refused_at_call_time() -> None:
    """R3: enforced by tool binding, not by prompt instruction."""
    agent, _ = build_agent(FakeLLM())
    with pytest.raises(ToolNotBoundError):
        await agent.runtime.dispatcher.call("list_candidate_profiles", program_id=uuid4())


def test_assessment_cannot_be_built_above_the_draft_ceiling() -> None:
    with pytest.raises(AutonomyCeilingError):
        AgentRuntime(
            agent=AgentName.ASSESSMENT,
            dispatcher=bind(toolset_for(AgentName.ASSESSMENT), PortBundle()),
            llm=FakeLLM(),
            autonomy=AutonomyLevel.ACT,
        )


def test_the_agent_refuses_another_agents_runtime() -> None:
    with pytest.raises(ValueError, match="assessment runtime"):
        AssessmentAgent(
            runtime=AgentRuntime(
                agent=AgentName.SOURCING,
                dispatcher=bind(toolset_for(AgentName.SOURCING), PortBundle()),
                llm=FakeLLM(),
            )
        )


async def test_every_draft_writes_an_audit_row_in_the_same_transaction() -> None:
    """§11: every state transition writes an AuditEvent — actor, action, before,
    after. `before` is None because the draft is the moment the artifact begins."""
    agent, sink = build_agent(FakeLLM(responses=["Batch MLEC-CSE-B3."]))
    actor = uuid4()
    await agent.draft_assessment_request(
        PROGRAM_ID, a_spec(), actor_id=actor, actor_persona=Persona.MANAGER
    )
    _, event = sink.saved[0]
    assert event.actor_id == actor
    assert event.actor_persona is Persona.MANAGER
    assert event.action == "agent.draft_saved"
    assert event.before is None
    assert event.after is not None
    assert event.after["autonomy"] == AutonomyLevel.DRAFT.value
