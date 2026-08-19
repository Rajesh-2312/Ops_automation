"""The Reporting agent. CLAUDE.md §8: "Governance report, feedback synthesis,
college summaries". Ceiling: Draft.

Three things this file is arranged to prove, in descending order of how
expensive they are to get wrong:

* **§12's figure assertion.** "Assert structure AND absence of fabricated
  figures — compare every number in generated text against the structured
  input." A governance report is the artifact most likely to be forwarded to a
  college unread, so a fabricated percentage in one is a fabrication with an
  audience. Tested by feeding the model an invented figure and asserting the
  draft is refused and nothing is saved.

* **§14 Q3 is carried, not answered.** Approval authority for college-facing
  artifacts is an open question. The agent must flag it and must not resolve it,
  so there is a test asserting the flag is present and another asserting no
  persona is named as approver anywhere in the draft.

* **R3 and the Draft ceiling.** No release-capable tool, no ceiling above Draft,
  and a saved artifact that can only be DRAFT.

The facts objects are the real ones from `app.services.reporting.assembly`.
`app/agents/reporting.py` accepts them through a `ReportFacts` protocol rather
than importing them — the layering matters and is asserted here too — so using
the real types is how the seam gets checked rather than assumed.
"""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents.grounding import UngroundedFigureError, collect_grounded_values, figures_in
from app.agents.ports import DocumentSnapshot, RetrievedPassage
from app.agents.reporting import (
    APPROVAL_AUTHORITY_OPEN_QUESTION,
    OPEN_TASK_STATUSES,
    ReportFacts,
    ReportingAgent,
)
from app.agents.runtime import AgentRuntime, AutonomyCeilingError
from app.agents.tools import AgentName, PortBundle, ToolEffect, bind, toolset_for
from app.domain.enums import (
    ArtifactState,
    ArtifactType,
    AutonomyLevel,
    LLMTask,
    Persona,
    ProgramStage,
    ProgramType,
    TaskStatus,
)
from app.services.reporting.assembly import (
    AssessmentFacts,
    BatchFacts,
    CollegeSummary,
    FeedbackEntry,
    ProgramFacts,
    ProgramSummaryLine,
    ReportPeriod,
    StudentAttendanceFacts,
    TaskFacts,
    TrainerCostLine,
    TrainerCostSection,
    TrainerDeliveryFacts,
    assemble_governance_report,
    summarise_college,
    synthesise_feedback,
)
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeDraftSink,
    FakeLLM,
    FakeProgramPort,
    FakeRetrievalPort,
    a_program,
    a_task,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTING_MODULE = REPO_ROOT / "app" / "agents" / "reporting.py"

COLLEGE_ID = uuid4()
PERIOD = ReportPeriod(start=dt.date(2026, 7, 1), end=dt.date(2026, 7, 31))


# --- fixtures: the real service objects --------------------------------------


def a_governance_report(*, with_cost: bool = False):  # noqa: ANN201
    """A real `GovernanceReport`, assembled by the real service function.

    Not a stub with an `as_payload()`. The agent's whole contract is "quote this
    payload and nothing else", and a stub payload would let a shape drift between
    what the service produces and what the agent is tested against.
    """
    cost = None
    if with_cost:
        cost = TrainerCostSection(
            lines=(
                TrainerCostLine(
                    trainer_name="VEMA PRUDHVI SAI",
                    trainer_pan="ABCDE1234F",
                    period_start=PERIOD.start,
                    period_end=PERIOD.end,
                    net=Decimal(14035),
                    invoice_no="BCDP/26-27/JUL1",
                ),
            )
        )
    return assemble_governance_report(
        program=ProgramFacts(
            program_id=PROGRAM_ID,
            program_name="bCAP Cohort A",
            program_type=ProgramType.BCAP,
            stage=ProgramStage.ACTIVE_MONITORING,
            college_id=COLLEGE_ID,
            college_name="Malineni Lakshmaiah",
        ),
        period=PERIOD,
        batches=(BatchFacts(batch_id=uuid4(), name="Batch 1", student_count=60),),
        trainers=(
            TrainerDeliveryFacts(
                deployment_id=uuid4(),
                trainer_name="VEMA PRUDHVI SAI",
                batch_name="Batch 1",
                days_in_period=31,
                marked_days=31,
                present_days=28,
                absent_days=3,
            ),
        ),
        student_attendance=StudentAttendanceFacts(sessions=40, present=35, absent=5),
        assessments=AssessmentFacts(conducted=2, with_report=2),
        observations=1,
        tasks=TaskFacts(open=3, overdue=1, blocked=0, done=9),
        feedback=(
            FeedbackEntry(
                source="Mid-programme survey",
                collected_on=dt.date(2026, 7, 15),
                summary_score=Decimal("4.2"),
                response_count=48,
            ),
        ),
        trainer_cost=cost,
    )


def a_college_summary() -> CollegeSummary:
    return summarise_college(
        college_id=COLLEGE_ID,
        college_name="Malineni Lakshmaiah",
        period=PERIOD,
        programs=(
            ProgramSummaryLine(
                program_id=PROGRAM_ID,
                program_name="bCAP Cohort A",
                program_type=ProgramType.BCAP,
                stage=ProgramStage.ACTIVE_MONITORING,
                batch_count=2,
                trainer_count=3,
                incomplete_tracksheets=1,
            ),
        ),
    )


def build_agent(
    llm: FakeLLM,
    *,
    tasks: Sequence[object] = (),
    documents: Sequence[DocumentSnapshot] = (),
    passages: Sequence[RetrievedPassage] = (),
    program: object | None = None,
) -> tuple[ReportingAgent, FakeDraftSink]:
    sink = FakeDraftSink()
    ports = PortBundle(
        programs=FakeProgramPort(
            program=a_program(ProgramStage.ACTIVE_MONITORING) if program is None else program,  # type: ignore[arg-type]
            tasks=tasks,  # type: ignore[arg-type]
            documents=documents,
        ),
        retrieval=FakeRetrievalPort(passages=passages),
        drafts=sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.REPORTING,
        dispatcher=bind(toolset_for(AgentName.REPORTING), ports),
        llm=llm,
    )
    return ReportingAgent(runtime=runtime), sink


# --- R3 and the Draft ceiling -------------------------------------------------


def test_the_reporting_toolset_exposes_no_release_capable_tool() -> None:
    """§12: "assert no agent toolset exposes a release-capable tool".

    A governance report is the artifact most likely to be shared outside byteXL,
    which makes this the toolset where a `send_email` would do the most damage.
    """
    toolset = toolset_for(AgentName.REPORTING)
    assert toolset.effects <= {ToolEffect.READ, ToolEffect.SAVE_DRAFT}
    for name in toolset.names:
        assert name == "save_draft" or name.startswith(("read_", "list_", "get_", "search_"))
    for forbidden in (
        "send_email",
        "send_whatsapp",
        "post_message",
        "mark_released",
        "share_with_college",
        "publish_report",
    ):
        assert forbidden not in toolset.names


def test_reporting_cannot_be_built_above_the_draft_ceiling() -> None:
    with pytest.raises(AutonomyCeilingError):
        AgentRuntime(
            agent=AgentName.REPORTING,
            dispatcher=bind(toolset_for(AgentName.REPORTING), PortBundle()),
            llm=FakeLLM(),
            autonomy=AutonomyLevel.ACT,
        )


def test_the_reporting_agent_refuses_a_runtime_for_another_agent() -> None:
    with pytest.raises(ValueError, match="reporting runtime"):
        ReportingAgent(
            runtime=AgentRuntime(
                agent=AgentName.INTAKE,
                dispatcher=bind(toolset_for(AgentName.INTAKE), PortBundle()),
                llm=FakeLLM(),
            )
        )


async def test_every_report_lands_in_draft() -> None:
    """R4: DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED, with a human at the
    approval step. `SavedDraft` cannot represent any other state."""
    agent, sink = build_agent(FakeLLM(responses=["Delivery proceeded as planned."]))
    outcome = await agent.draft_governance_report(PROGRAM_ID, a_governance_report())
    assert outcome.saved.state is ArtifactState.DRAFT
    assert len(sink.saved) == 1


# --- §14 Q3: carried, never answered -----------------------------------------


async def test_a_governance_report_carries_the_open_approval_question() -> None:
    """§14 Q3 — "Approval authority for college-facing comms: Manager or Senior
    Manager?" — is open, and the draft says so before anyone clicks Approve.

    `APPROVAL_AUTHORITY` has no entry for `GOVERNANCE_REPORT`, so
    `approver_personas()` raises. Discovering that at the approval attempt is a
    worse experience than being told at drafting time, and inventing an answer
    to avoid the block would be worse than either.
    """
    agent, sink = build_agent(FakeLLM(responses=["Delivery proceeded as planned."]))
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())
    draft, _ = sink.saved[0]
    assert APPROVAL_AUTHORITY_OPEN_QUESTION in draft.flags
    assert "§14" in APPROVAL_AUTHORITY_OPEN_QUESTION


async def test_a_college_summary_carries_it_too() -> None:
    """A summary shared with a college is exactly the college-facing
    communication Q3 is about — `drafts.py` types it `PROGRAM_DOCUMENT` for the
    same reason."""
    agent, sink = build_agent(FakeLLM(responses=["Two programmes are running."]))
    await agent.draft_college_summary(a_college_summary(), college_name="Malineni Lakshmaiah")
    draft, _ = sink.saved[0]
    assert APPROVAL_AUTHORITY_OPEN_QUESTION in draft.flags


def test_the_open_question_names_no_approver() -> None:
    """The moment this string names a persona, an agent module has answered an
    open question on the owner's behalf — and `APPROVAL_AUTHORITY` still would
    not have the entry, so approval would fail anyway."""
    for persona in (Persona.MANAGER, Persona.SENIOR_MANAGER):
        assert persona.value not in APPROVAL_AUTHORITY_OPEN_QUESTION
    assert "Manager or a Senior Manager" in APPROVAL_AUTHORITY_OPEN_QUESTION


def test_drafting_a_report_does_not_resolve_the_open_question() -> None:
    """The strongest form of "carried, not answered": after this agent exists,
    the approval layer still refuses to name an approver for either type.

    Asserted against `approver_personas()` itself rather than against the agent's
    own flag text, because the way Q3 would actually get resolved by accident is
    somebody adding an `APPROVAL_AUTHORITY` entry to make a draft approvable.
    Answering it means answering it with the owner and adding the entry
    deliberately — not here, and not by picking whichever persona makes a test
    pass.
    """
    from app.services.approval.state_machine import (
        ApprovalAuthorityUndefinedError,
        approver_personas,
    )

    for artifact_type in (ArtifactType.GOVERNANCE_REPORT, ArtifactType.PROGRAM_DOCUMENT):
        with pytest.raises(ApprovalAuthorityUndefinedError):
            approver_personas(artifact_type)


# --- §12: every number in the text came from the structured input -------------


async def test_a_fabricated_percentage_is_refused() -> None:
    """The failure a governance report makes expensive: a plausible figure in a
    document somebody has already forwarded.

    Student attendance is 35 of 40 sessions. "88%" is arithmetically defensible
    and was not computed by anything, so the draft is refused rather than
    repaired.
    """
    agent, sink = build_agent(
        FakeLLM(responses=["Student attendance held at 88% across the period."])
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_governance_report(PROGRAM_ID, a_governance_report())

    assert exc.value.context == "reporting.governance_report"
    assert not sink.saved


async def test_every_figure_in_a_governance_narrative_is_checked() -> None:
    """§12, as a property rather than an example.

    The grounded set is rebuilt from the draft's own `grounded_in` and every
    digit run in the body is required to be in it — the same comparison
    `assert_grounded` made, re-run from the saved artifact so the assertion does
    not depend on trusting the code under test.
    """
    body = (
        "60 students are enrolled in Batch 1. 28 of 31 days were marked present, 2 "
        "assessments were conducted and 3 tasks remain open."
    )
    agent, sink = build_agent(FakeLLM(responses=[body]))
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())

    draft, _ = sink.saved[0]
    allowed = collect_grounded_values(draft.grounded_in)
    written = figures_in(draft.body)
    assert written, "the fixture must contain figures for this to assert anything"
    assert [value for _, value in written if value not in allowed] == []


async def test_a_feedback_average_the_model_recomputed_is_refused() -> None:
    """`synthesise_feedback()` averages in `Decimal`, once. A model that restates
    the average as 4.3 has produced a number, and a feedback score is quoted in
    college meetings."""
    synthesis = synthesise_feedback(
        (
            FeedbackEntry(source="A", summary_score=Decimal("4.2"), response_count=48),
            FeedbackEntry(source="B", summary_score=Decimal("4.4"), response_count=12),
        )
    )
    agent, sink = build_agent(FakeLLM(responses=["Feedback averages 4.35 across collections."]))
    with pytest.raises(UngroundedFigureError):
        await agent.draft_feedback_synthesis(PROGRAM_ID, synthesis, program_name="bCAP Cohort A")
    assert not sink.saved


async def test_a_feedback_average_may_be_quoted_as_computed() -> None:
    synthesis = synthesise_feedback(
        (
            FeedbackEntry(source="A", summary_score=Decimal("4.2"), response_count=48),
            FeedbackEntry(source="B", summary_score=Decimal("4.4"), response_count=12),
        )
    )
    assert synthesis.average_score == Decimal("4.30")
    agent, sink = build_agent(
        FakeLLM(responses=["The average across 2 scored collections is 4.30."])
    )
    outcome = await agent.draft_feedback_synthesis(
        PROGRAM_ID, synthesis, program_name="bCAP Cohort A"
    )
    assert outcome.saved.state is ArtifactState.DRAFT
    assert len(sink.saved) == 1


async def test_a_partial_average_is_flagged() -> None:
    """An average over three of eight collections presented as "the feedback
    score" is the figure that survives into a college meeting unchallenged."""
    synthesis = synthesise_feedback(
        (
            FeedbackEntry(source="A", summary_score=Decimal("4.2")),
            FeedbackEntry(source="B"),
        )
    )
    agent, sink = build_agent(FakeLLM(responses=["1 of 2 collections carried a score of 4.20."]))
    await agent.draft_feedback_synthesis(PROGRAM_ID, synthesis, program_name="bCAP Cohort A")
    draft, _ = sink.saved[0]
    assert any("covers 1 of 2 collection" in flag for flag in draft.flags)


async def test_the_programme_name_is_grounded_not_prompt_only() -> None:
    """A programme called "bCAP 2026" licenses the model to write 2026. Putting
    the name in the system prompt instead would make that a grounding
    violation."""
    synthesis = synthesise_feedback((FeedbackEntry(source="A", summary_score=Decimal("4.2")),))
    agent, sink = build_agent(FakeLLM(responses=["bCAP 2026 scored 4.20."]))
    outcome = await agent.draft_feedback_synthesis(PROGRAM_ID, synthesis, program_name="bCAP 2026")
    assert outcome.saved.state is ArtifactState.DRAFT


# --- R2 in a reporting module -------------------------------------------------


def test_the_reporting_agent_contains_no_arithmetic() -> None:
    """A governance report is the one artifact that carries both delivery facts
    and commercials, so R2 has to hold here as firmly as in the payout agent.

    `TrainerCostSection` refuses to total its own lines; this asserts the agent
    that narrates it cannot either. `|` is exempt — it is type-union syntax.
    """
    arithmetic = (
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.MatMult,
    )
    tree = ast.parse(REPORTING_MODULE.read_text(encoding="utf-8"))
    operators = [
        f"line {node.lineno}: {type(node.op).__name__}"
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, arithmetic)
    ]
    calls = [
        f"line {node.lineno}: {node.func.id}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"sum", "round", "float"}
    ]
    assert operators == [], f"arithmetic in the reporting agent (CLAUDE.md R2): {operators}"
    assert calls == [], f"money-shaped builtins in the reporting agent (CLAUDE.md R2): {calls}"


async def test_a_trainer_cost_total_the_model_invented_is_refused() -> None:
    """One cost line of 14,035 and a "total of 14,035" would pass, so the test
    uses a figure nothing produced. R2: the total belongs in the engine."""
    agent, sink = build_agent(
        FakeLLM(responses=["Trainer cost for the period totalled 28,070 across the programme."])
    )
    with pytest.raises(UngroundedFigureError):
        await agent.draft_governance_report(PROGRAM_ID, a_governance_report(with_cost=True))
    assert not sink.saved


async def test_a_commercial_report_is_flagged_as_commercial() -> None:
    """§4: trainer cost is Senior Manager and Manager only. A reviewer must not
    have to open the payload to find out which kind of report they are holding."""
    agent, sink = build_agent(FakeLLM(responses=["Delivery proceeded as planned."]))
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report(with_cost=True))
    draft, _ = sink.saved[0]
    assert any(flag.startswith("COMMERCIAL") for flag in draft.flags)


async def test_a_non_commercial_report_is_not_flagged_as_commercial() -> None:
    agent, sink = build_agent(FakeLLM(responses=["Delivery proceeded as planned."]))
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())
    draft, _ = sink.saved[0]
    assert not any(flag.startswith("COMMERCIAL") for flag in draft.flags)


def test_a_college_summary_carries_no_commercials_at_all() -> None:
    """§4: the view an LDE Executive works from daily. Not even an empty section
    — a present-but-empty one is a refactor away from present-and-full."""
    payload = a_college_summary().as_payload()
    rendered = str(payload).lower()
    for term in ("net", "invoice", "rate", "cost", "payout"):
        assert term not in rendered


# --- the operational overlay the agent reads for itself -----------------------


async def test_unsigned_documents_are_read_and_flagged() -> None:
    """The half of a governance report a delivery table does not carry.

    A report that says delivery went well while the work order is unsigned will
    be contradicted later — and per §7 an unsigned work order blocks the payout
    cycle outright.
    """
    documents = (
        DocumentSnapshot(document_id=uuid4(), title="Work order", status="sent", signed=False),
        DocumentSnapshot(document_id=uuid4(), title="MoU", status="signed", signed=True),
    )
    agent, sink = build_agent(
        FakeLLM(responses=["Delivery proceeded; the work order is unsigned."]),
        documents=documents,
    )
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())

    draft, _ = sink.saved[0]
    operations = draft.payload["operations"]
    assert operations["unsigned_documents"] == ["Work order"]  # type: ignore[index]
    assert any("unsigned document obligation" in flag for flag in draft.flags)


async def test_open_tasks_are_counted_and_blocked_counts_as_open() -> None:
    """BLOCKED is not finished, and a report that omits blocked work reads as
    though nothing is stuck."""
    tasks = (
        a_task("Chase work order", status=TaskStatus.BLOCKED),
        a_task("Collect attendance", status=TaskStatus.PENDING),
        a_task("Issue invoice", status=TaskStatus.DONE),
    )
    agent, sink = build_agent(FakeLLM(responses=["2 tasks remain open."]), tasks=tasks)
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())

    draft, _ = sink.saved[0]
    assert draft.payload["operations"]["open_task_count"] == 2  # type: ignore[index]
    assert TaskStatus.BLOCKED in OPEN_TASK_STATUSES
    assert TaskStatus.DONE not in OPEN_TASK_STATUSES


async def test_an_unreachable_programme_is_reported_as_absent_not_invented() -> None:
    """§4 RLS: `read_program` returns `None` when the programme is out of the
    session's scope. The draft says so; it does not fill the gap in."""
    agent, sink = build_agent(FakeLLM(responses=["Delivery proceeded."]), program=None)
    ports = PortBundle(
        programs=FakeProgramPort(program=None),
        retrieval=FakeRetrievalPort(),
        drafts=sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.REPORTING,
        dispatcher=bind(toolset_for(AgentName.REPORTING), ports),
        llm=FakeLLM(responses=["Delivery proceeded."]),
    )
    await ReportingAgent(runtime=runtime).draft_governance_report(PROGRAM_ID, a_governance_report())
    draft, _ = sink.saved[0]
    assert draft.payload["operations"]["college_name"] is None  # type: ignore[index]
    assert any("not readable in this session's scope" in flag for flag in draft.flags)


# --- §9: policy passages supply words, never figures --------------------------


async def test_a_figure_lifted_from_a_retrieved_passage_is_refused() -> None:
    """§9: "Structured facts (dates, amounts, counts) are never retrieved from
    RAG." The passage text reaches the prompt; only its citation label grounds."""
    passage = RetrievedPassage(
        document_title="Reporting SOP",
        section="Cadence",
        text="Governance reports are shared within 14 working days of period end.",
    )
    # 14 appears nowhere in the report facts, deliberately. `collect_grounded_values`
    # scans inside strings, so a small integer that also happens to be a day or a
    # month in some date on the payload would be grounded for an unrelated reason
    # and this test would assert nothing.
    assert Decimal(14) not in collect_grounded_values(a_governance_report().as_payload())

    agent, sink = build_agent(
        FakeLLM(responses=["The report is due within 14 working days of period end."]),
        passages=(passage,),
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_governance_report(PROGRAM_ID, a_governance_report())

    assert any(v.value == Decimal(14) for v in exc.value.violations)
    assert not sink.saved


async def test_the_citation_label_is_grounded_and_the_body_is_not() -> None:
    passage = RetrievedPassage(
        document_title="Reporting SOP",
        section="Structure",
        text="A governance report covers delivery, attendance and feedback.",
    )
    llm = FakeLLM(responses=["Delivery, attendance and feedback are covered."])
    agent, sink = build_agent(llm, passages=(passage,))
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())

    prompt = str(llm.calls[0]["user"])
    assert "A governance report covers delivery" in prompt
    draft, _ = sink.saved[0]
    assert draft.grounded_in["sources"] == [{"document": "Reporting SOP", "section": "Structure"}]
    assert "covers delivery, attendance" not in str(draft.grounded_in)


# --- §2: route by task, not by default ----------------------------------------


async def test_a_governance_report_routes_to_the_frontier_tier() -> None:
    """§2 names governance reports as frontier work. The report is read by a
    college and a wrong emphasis is expensive to retract."""
    llm = FakeLLM(responses=["Delivery proceeded as planned."])
    agent, _ = build_agent(llm)
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())
    assert llm.tasks_called == [LLMTask.GOVERNANCE_REPORT]
    assert llm.calls[0]["task"] is LLMTask.GOVERNANCE_REPORT


async def test_summaries_route_to_the_volume_tier() -> None:
    """§2: summaries are volume work. Routing a college summary to the frontier
    model would be paying frontier prices for a paragraph nobody outside byteXL
    reads."""
    llm = FakeLLM(responses=["Two programmes are running.", "Feedback is positive."])
    agent, _ = build_agent(llm)
    await agent.draft_college_summary(a_college_summary(), college_name="Malineni Lakshmaiah")
    await agent.draft_feedback_synthesis(
        PROGRAM_ID,
        synthesise_feedback((FeedbackEntry(source="A"),)),
        program_name="bCAP Cohort A",
    )
    assert llm.tasks_called == [LLMTask.SUMMARY, LLMTask.SUMMARY]


# --- artifact typing and the audit row ----------------------------------------


async def test_a_governance_report_is_typed_as_a_governance_report() -> None:
    """`ArtifactType`'s value doubles as the audit row's `entity_table` (§11), so
    the type decides which table the trail points at."""
    agent, sink = build_agent(FakeLLM(responses=["Delivery proceeded as planned."]))
    await agent.draft_governance_report(PROGRAM_ID, a_governance_report())
    draft, event = sink.saved[0]
    assert draft.artifact_type is ArtifactType.GOVERNANCE_REPORT
    assert event.entity_table == "governance_reports"


async def test_a_college_summary_is_not_filed_as_a_governance_report() -> None:
    """A college summary is not the periodic governance report; filing it as one
    would put it in `governance_reports` where nobody would look for it."""
    agent, sink = build_agent(FakeLLM(responses=["Two programmes are running."]))
    await agent.draft_college_summary(a_college_summary(), college_name="Malineni Lakshmaiah")
    draft, _ = sink.saved[0]
    assert draft.artifact_type is ArtifactType.PROGRAM_DOCUMENT
    assert draft.program_id is None


async def test_the_audit_row_records_the_agent_the_model_and_the_flags() -> None:
    """§11: actor, action, before, after, at — plus which agent and which model,
    which is the first question asked when a draft turns out to be wrong."""
    agent, sink = build_agent(FakeLLM(responses=["Delivery proceeded as planned."]))
    actor = uuid4()
    await agent.draft_governance_report(
        PROGRAM_ID, a_governance_report(), actor_id=actor, actor_persona=Persona.MANAGER
    )
    _, event = sink.saved[0]
    assert event.actor_id == actor
    assert event.actor_persona is Persona.MANAGER
    assert event.action == "agent.draft_saved"
    assert event.before is None
    assert event.after is not None
    assert event.after["agent"] == "reporting"
    assert event.after["model"] == "fake/frontier"
    assert event.after["autonomy"] == AutonomyLevel.DRAFT.value


async def test_the_invocation_records_the_reads_it_made() -> None:
    """§11's "tools called" half, joined to the LLM half by `AgentRuntime`."""
    agent, _ = build_agent(FakeLLM(responses=["Delivery proceeded as planned."]))
    outcome = await agent.draft_governance_report(PROGRAM_ID, a_governance_report())
    assert [call.tool for call in outcome.invocation.tools_called] == [
        "read_program",
        "list_program_tasks",
        "list_program_documents",
        "search_corpus",
    ]
    assert all(call.effect is ToolEffect.READ for call in outcome.invocation.tools_called)


# --- the layering that keeps the import graph acyclic -------------------------


def _service_imports(path: Path, prefix: str) -> list[str]:
    """Module-scope imports in `path` whose target starts with `prefix`."""
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(prefix):
            found.append(f"{path.name}:{node.lineno} from {node.module}")
        if isinstance(node, ast.Import):
            found.extend(
                f"{path.name}:{node.lineno} import {alias.name}"
                for alias in node.names
                if alias.name.startswith(prefix)
            )
    return found


def test_no_agent_imports_the_reporting_service() -> None:
    """The import cycle this module's `ReportFacts` protocol exists to avoid.

    `app/services/reporting/__init__.py` imports `drafts`, which imports
    `narration`, which imports `app.agents.grounding` — so importing
    `app.services.reporting` from anywhere under `app/agents/` completes a cycle
    the moment `app/agents/__init__.py` pulls that agent in, and the failure is a
    partially-initialised module rather than a clear error.

    Scoped to the reporting package rather than to all of `app/services/`,
    because that is where the cycle actually is: other specialists legitimately
    import services that do not import back.
    """
    offenders = [
        entry
        for path in sorted((REPO_ROOT / "app" / "agents").rglob("*.py"))
        for entry in _service_imports(path, "app.services.reporting")
    ]
    assert offenders == [], (
        "app/agents/ must not import app.services.reporting — it imports back into "
        f"app.agents.grounding and the cycle closes on the next __init__ edit: {offenders}"
    )


def test_the_reporting_agent_imports_no_service_at_all() -> None:
    """Belt and braces on the module this file owns.

    The protocol makes every service import unnecessary here, so the honest
    assertion for this one file is zero — a later import of some other service
    would be the first step back towards the cycle.
    """
    assert _service_imports(REPORTING_MODULE, "app.services") == []


def test_the_real_service_objects_satisfy_the_reports_protocol() -> None:
    """The seam the layering rule costs us, asserted rather than assumed.

    All three §8 Reporting outputs go through `ReportFacts`, so if `as_payload()`
    ever changes shape on the service side this fails here rather than at the
    first draft.
    """
    assert isinstance(a_governance_report(), ReportFacts)
    assert isinstance(a_college_summary(), ReportFacts)
    assert isinstance(synthesise_feedback((FeedbackEntry(source="A"),)), ReportFacts)


async def test_the_narrative_is_grounded_against_the_payload_that_gets_frozen() -> None:
    """R1 and R4 meet here: the prose may only quote figures from the same
    mapping the approval layer hashes, so "what was approved" and "what the
    narrative could say" cannot drift apart."""
    report = a_governance_report()
    agent, sink = build_agent(FakeLLM(responses=["60 students are enrolled."]))
    await agent.draft_governance_report(PROGRAM_ID, report)
    draft, _ = sink.saved[0]
    assert draft.payload["report"] == report.as_payload()
    assert draft.grounded_in["report"] == report.as_payload()
