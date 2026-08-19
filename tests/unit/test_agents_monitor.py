"""The Delivery Monitor. CLAUDE.md §8: "Attendance, usage, syllabus anomalies,
risk scoring". Ceiling: **Alert (internal only)** — autonomy level 1, Observe.

The assertions divide the way the module does, and the division is the point of
the whole file. `app/services/monitoring/` and `app/services/escalation/` already
detect, score, band and escalate, deterministically and without a model. What is
tested here is that the agent sitting on top of them **adds language and nothing
else**: it may not re-score, may not re-detect, may not invent a figure, may not
describe an unmeasured signal as healthy, and may not address anybody outside
byteXL. §12 asks for the first and the last of those explicitly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import uuid4

import pytest

from app.agents.grounding import UngroundedFigureError
from app.agents.monitor import (
    SIGNAL_FAMILY_CODES,
    FabricatedAnomalyError,
    MonitorAgent,
    SignalFamily,
    SignalState,
    assert_only_detected_anomalies,
    briefing_payload,
    signal_coverage,
)
from app.agents.ports import Draft
from app.agents.runtime import AgentRuntime, AutonomyCeilingError
from app.agents.tools import AgentName, PortBundle, bind, toolset_for
from app.domain.attendance import expand_period
from app.domain.enums import (
    ArtifactType,
    AttendanceMark,
    AutonomyLevel,
    LLMTask,
    Persona,
    ProgramStage,
    ProgramType,
    TaskStatus,
)
from app.domain.risk import AnomalyCode, RiskBand, SlaMetric
from app.services.escalation import SlaFacts, evaluate_sla
from app.services.monitoring import AttendanceSignal, MonitorContext, SyllabusSignal, assess_risk
from app.services.monitoring.signals import UsageSignal
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeDraftSink,
    FakeLLM,
    FakeProgramPort,
    a_contact,
    a_program,
    a_task,
)

COLLEGE_ID = uuid4()
AS_OF = dt.datetime(2026, 7, 20, 6, 0, tzinfo=dt.UTC)


def a_context(
    *,
    program_type: ProgramType = ProgramType.CRT,
    with_attendance: bool = True,
    with_usage: bool = False,
    with_syllabus: bool = False,
    marks: dict[dt.date, AttendanceMark] | None = None,
) -> MonitorContext:
    """One observation window. Usage and syllabus default to absent, which is the
    live shape today: §14 Q6 is open and no column stores a completion reading."""
    attendance = None
    if with_attendance:
        attendance = AttendanceSignal(
            period_start=dt.date(2026, 7, 1),
            period_end=dt.date(2026, 7, 31),
            days=tuple(expand_period(dt.date(2026, 7, 1), dt.date(2026, 7, 31), marks)),
            elapsed_through=dt.date(2026, 7, 19),
            last_marked_at=dt.datetime(2026, 7, 19, 12, 0, tzinfo=dt.UTC),
        )
    return MonitorContext(
        program_id=PROGRAM_ID,
        college_id=COLLEGE_ID,
        program_type=program_type,
        as_of=AS_OF,
        attendance=attendance,
        usage=(
            UsageSignal(enrolled_learners=60, active_learners=55, sessions_held=8)
            if with_usage
            else None
        ),
        syllabus=(
            SyllabusSignal(completion_percent=Decimal(50), expected_percent=Decimal(55))
            if with_syllabus
            else None
        ),
    )


def a_clean_context() -> MonitorContext:
    """Attendance marked present on every elapsed day: no anomaly fires."""
    marks = {
        dt.date(2026, 7, day): AttendanceMark.PRESENT for day in range(1, 20)
    }  # 1..19 inclusive
    return a_context(marks=marks)


# --- signal coverage: what was looked at, not what was found -----------------


def test_absent_signals_are_reported_absent_not_healthy() -> None:
    """The engine's rule in prose form: "not measured is not zero"."""
    coverage = {row.family: row.coverage for row in signal_coverage(a_context())}
    assert coverage[SignalFamily.ATTENDANCE] is SignalState.OBSERVED
    assert coverage[SignalFamily.USAGE] is SignalState.NOT_MEASURED
    assert coverage[SignalFamily.SYLLABUS] is SignalState.NOT_MEASURED


def test_coverage_reports_presence_never_content() -> None:
    """A supplied-but-terrible usage signal is OBSERVED, not "bad"."""
    ctx = a_context(with_usage=True)
    usage = next(row for row in signal_coverage(ctx) if row.family is SignalFamily.USAGE)
    assert usage.is_measured


def test_coverage_is_reported_in_a_fixed_order() -> None:
    """§8's own ordering, so a briefing reads the same way every week."""
    assert [row.family for row in signal_coverage(a_context())] == list(SignalFamily)


def test_every_anomaly_code_belongs_to_exactly_one_signal_family() -> None:
    """Drift guard. A new AnomalyCode upstream must be classified here, or the
    briefing would silently stop reporting that it could not be evaluated."""
    grouped = [code for codes in SIGNAL_FAMILY_CODES.values() for code in codes]
    assert sorted(grouped, key=str) == sorted(AnomalyCode, key=str)
    assert len(grouped) == len(set(grouped))


def test_the_dark_codes_are_the_usage_and_syllabus_ones() -> None:
    """§14 Q6 is open and no column stores syllabus completion, so five of the nine
    detectors — and the five codes below — cannot fire today. This pins which: if a
    usage table ever lands, this test is the reminder that the briefing's coverage
    story changes with it."""
    dark = tuple(
        code for row in signal_coverage(a_context()) if not row.is_measured for code in row.codes
    )
    assert set(dark) == {
        AnomalyCode.USAGE_LOW_ACTIVE_SHARE,
        AnomalyCode.USAGE_NO_ACTIVITY,
        AnomalyCode.SYLLABUS_BEHIND_SCHEDULE,
        AnomalyCode.SYLLABUS_STALLED,
        AnomalyCode.SYLLABUS_IMPLAUSIBLE,
    }


# --- the second guard: an invented finding carries no digits ----------------


def test_a_finding_that_did_not_fire_is_refused() -> None:
    with pytest.raises(FabricatedAnomalyError) as exc:
        assert_only_detected_anomalies(
            "No learner activity at all: usage_no_activity on this batch.",
            [AnomalyCode.ATTENDANCE_UNMARKED_DAYS],
            "monitor.briefing",
        )
    assert exc.value.codes == (AnomalyCode.USAGE_NO_ACTIVITY,)
    assert exc.value.context == "monitor.briefing"


def test_a_finding_that_did_fire_is_allowed() -> None:
    assert_only_detected_anomalies(
        "attendance_unmarked_days fired for 4 days.",
        [AnomalyCode.ATTENDANCE_UNMARKED_DAYS],
        "monitor.briefing",
    )


def test_ordinary_prose_does_not_trip_the_finding_guard() -> None:
    """The check must be precise in the direction that matters, or it gets muted."""
    assert_only_detected_anomalies(
        "Attendance is stale and usage is low; the syllabus was not observed.",
        [],
        "monitor.briefing",
    )


# --- the payload: exactly what the model is permitted to quote --------------


def test_the_payload_carries_the_score_as_a_string_not_a_float() -> None:
    """R6's discipline outside money: the model must quote the score exactly."""
    ctx = a_context()
    payload = briefing_payload(assess_risk(ctx), signal_coverage(ctx))
    risk = payload["risk"]
    assert isinstance(risk, dict)
    assert isinstance(risk["score"], str)


def test_the_payload_names_unmeasured_families() -> None:
    ctx = a_context()
    payload = briefing_payload(assess_risk(ctx), signal_coverage(ctx))
    assert payload["not_measured"] == ["usage", "syllabus"]


def test_the_payload_never_carries_the_context_labels() -> None:
    """§6: trainer identity is PAN. It is not needed to write a paragraph about a
    program, so it does not go to the gateway."""
    ctx = MonitorContext(
        program_id=PROGRAM_ID,
        college_id=COLLEGE_ID,
        program_type=ProgramType.CRT,
        as_of=AS_OF,
        attendance=a_context().attendance,
        labels=(("trainer_pan", "BCDPS1234K"), ("batch", "B3")),
    )
    payload = briefing_payload(assess_risk(ctx), signal_coverage(ctx))
    assert "BCDPS1234K" not in str(payload)


def test_a_program_out_of_reach_is_reported_as_none_not_guessed() -> None:
    """§4 RLS: a program the caller cannot read has no college name to state."""
    ctx = a_context()
    payload = briefing_payload(assess_risk(ctx), signal_coverage(ctx), program=None)
    assert payload["program"] is None


# --- the agent ---------------------------------------------------------------


def build_agent(
    llm: FakeLLM,
    *,
    contacts: tuple = (),
    tasks: tuple = (),
) -> MonitorAgent:
    ports = PortBundle(
        programs=FakeProgramPort(
            program=a_program(ProgramStage.ACTIVE_MONITORING),
            contacts=contacts,
            tasks=tasks,
        )
    )
    runtime = AgentRuntime(
        agent=AgentName.MONITOR,
        dispatcher=bind(toolset_for(AgentName.MONITOR), ports),
        llm=llm,
        autonomy=AutonomyLevel.OBSERVE,
    )
    return MonitorAgent(runtime=runtime)


async def test_the_briefing_explains_a_computed_assessment() -> None:
    ctx = a_context()
    assessment = assess_risk(ctx)
    llm = FakeLLM(responses=["Attendance for July has 19 unmarked elapsed days."])
    agent = build_agent(llm)

    briefing = await agent.explain_alert(assessment, ctx, audience=(Persona.MANAGER,))
    assert briefing.narrative.startswith("Attendance for July")
    assert briefing.assessment is assessment


async def test_the_headline_is_deterministic_and_not_model_output() -> None:
    """The authoritative reading is rendered by build_alert(); the prose sits
    beside it, never instead of it."""
    ctx = a_context()
    assessment = assess_risk(ctx)
    agent = build_agent(FakeLLM(responses=["19 days are unmarked."]))
    briefing = await agent.explain_alert(assessment, ctx, audience=(Persona.MANAGER,))

    assert briefing.headline == briefing.alert.headline
    assert str(assessment.score) in briefing.headline
    assert briefing.narrative not in briefing.headline


async def test_the_agent_does_not_rescore() -> None:
    """The score and band on the briefing are the ones handed in, byte for byte."""
    ctx = a_context()
    assessment = assess_risk(ctx)
    agent = build_agent(FakeLLM(responses=["19 unmarked days."]))
    briefing = await agent.explain_alert(assessment, ctx, audience=(Persona.MANAGER,))

    assert briefing.assessment.score == assessment.score
    assert briefing.alert.band is assessment.band
    assert assessment.band is RiskBand.HIGH  # one CRITICAL anomaly, weight 8


async def test_a_score_the_model_invented_is_refused() -> None:
    """§12: compare every number in generated text against the structured input."""
    ctx = a_context()
    agent = build_agent(FakeLLM(responses=["Delivery risk is 47, which is severe."]))
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.explain_alert(assess_risk(ctx), ctx, audience=(Persona.MANAGER,))
    assert exc.value.context == "monitor.briefing"


async def test_a_finding_invented_for_an_unmeasured_family_is_refused() -> None:
    """The failure this agent must never have: usage was never supplied, so a
    usage finding cannot have been observed. It carries no digits, so grounding
    alone would have let it through."""
    ctx = a_context()
    agent = build_agent(
        FakeLLM(responses=["Attendance is behind and usage_no_activity is also firing."])
    )
    with pytest.raises(FabricatedAnomalyError) as exc:
        await agent.explain_alert(assess_risk(ctx), ctx, audience=(Persona.MANAGER,))
    assert AnomalyCode.USAGE_NO_ACTIVITY in exc.value.codes


async def test_a_clean_run_still_reports_the_unobserved_families() -> None:
    """Nothing fired, but two families were never looked at. The briefing says so
    rather than letting silence read as health."""
    ctx = a_clean_context()
    assessment = assess_risk(ctx)
    assert assessment.is_clean

    agent = build_agent(FakeLLM(responses=["Nothing fired on attendance this period."]))
    briefing = await agent.explain_alert(assessment, ctx, audience=(Persona.MANAGER,))
    assert briefing.unmeasured == (SignalFamily.USAGE, SignalFamily.SYLLABUS)
    assert briefing.grounded_in["not_measured"] == ["usage", "syllabus"]


async def test_an_escalation_decision_is_quoted_never_re_decided() -> None:
    """§8: the Escalation Engine is deterministic SLA rules, not LLM judgement."""
    ctx = a_context()
    assessment = assess_risk(ctx)
    decision = evaluate_sla(
        SlaFacts(
            subject_table="programs",
            subject_id=PROGRAM_ID,
            program_id=PROGRAM_ID,
            college_id=COLLEGE_ID,
            program_type=ProgramType.CRT,
            as_of=AS_OF,
            metrics={SlaMetric.DELIVERY_RISK_SCORE: assessment.score},
        )
    )
    assert decision.fired

    llm = FakeLLM(responses=["This has escalated to the manager tier."])
    agent = build_agent(llm)
    briefing = await agent.explain_alert(
        assessment, ctx, audience=(Persona.MANAGER,), escalations=decision
    )

    escalations = briefing.grounded_in["escalations"]
    assert isinstance(escalations, dict)
    rules = escalations["rules"]
    assert isinstance(rules, list)
    assert [rule["code"] for rule in rules] == [  # type: ignore[index]
        code.value for code in decision.codes()
    ]
    # The reason is SlaRule.explain() output, generated from the rule row itself.
    assert "Measured" in str(rules[0]["reason"])  # type: ignore[index]


async def test_an_escalation_for_another_program_is_refused() -> None:
    ctx = a_context()
    other = evaluate_sla(
        SlaFacts(
            subject_table="programs",
            subject_id=uuid4(),
            program_id=uuid4(),
            college_id=COLLEGE_ID,
            program_type=ProgramType.CRT,
            as_of=AS_OF,
        )
    )
    agent = build_agent(FakeLLM(responses=["ignored"]))
    with pytest.raises(ValueError, match="EscalationDecision is for program"):
        await agent.explain_alert(
            assess_risk(ctx), ctx, audience=(Persona.MANAGER,), escalations=other
        )


async def test_a_context_for_another_program_is_refused() -> None:
    """Reporting one program's anomalies under another program's name."""
    ctx = a_context()
    stray = MonitorContext(
        program_id=uuid4(),
        college_id=COLLEGE_ID,
        program_type=ProgramType.CRT,
        as_of=AS_OF,
        attendance=ctx.attendance,
    )
    agent = build_agent(FakeLLM(responses=["ignored"]))
    with pytest.raises(ValueError, match="MonitorContext is for program"):
        await agent.explain_alert(assess_risk(ctx), stray, audience=(Persona.MANAGER,))


# --- internal only, and no capability to be anything else -------------------


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
async def test_an_external_audience_is_refused_before_a_row_is_read(
    persona: Persona,
) -> None:
    """§8: the ceiling is Alert, INTERNAL ONLY. §4 puts both outside the chain."""
    ctx = a_context()
    llm = FakeLLM(responses=["never generated"])
    agent = build_agent(llm)
    with pytest.raises(ValueError, match="external persona"):
        await agent.explain_alert(assess_risk(ctx), ctx, audience=(persona,))
    assert llm.calls == []


async def test_an_external_contact_never_reaches_the_prompt() -> None:
    """Filtered in the consumer, so a port that returns a college cannot make the
    monitor write about one."""
    ctx = a_context()
    llm = FakeLLM(responses=["19 unmarked days."])
    agent = build_agent(
        llm,
        contacts=(
            a_contact(Persona.MANAGER, "R. Maroju"),
            a_contact(Persona.TRAINER, "VEMA PRUDHVI SAI"),
            a_contact(Persona.COLLEGE, "Malineni Principal"),
        ),
    )
    briefing = await agent.explain_alert(assess_risk(ctx), ctx, audience=(Persona.MANAGER,))

    prompt = str(llm.calls[0]["user"])
    assert "VEMA PRUDHVI SAI" not in prompt
    assert "Malineni Principal" not in prompt
    assert briefing.grounded_in["internal_staff_on_program"] == ["R. Maroju"]


async def test_only_open_monitoring_tasks_are_given_as_context() -> None:
    ctx = a_context()
    llm = FakeLLM(responses=["19 unmarked days."])
    agent = build_agent(
        llm,
        tasks=(
            a_task("Mark attendance daily", stage=ProgramStage.ACTIVE_MONITORING),
            a_task(
                "Collect signed MoU",
                stage=ProgramStage.ACQUISITION_SETUP,
                status=TaskStatus.DONE,
            ),
            a_task(
                "Close feedback loop",
                stage=ProgramStage.ACTIVE_MONITORING,
                status=TaskStatus.DONE,
            ),
        ),
    )
    briefing = await agent.explain_alert(assess_risk(ctx), ctx, audience=(Persona.MANAGER,))
    tasks = briefing.grounded_in["open_monitoring_tasks"]
    assert isinstance(tasks, list)
    assert [task["title"] for task in tasks] == ["Mark attendance daily"]  # type: ignore[index]


def test_the_monitor_holds_no_write_capability_at_all() -> None:
    """R3, and §8's ceiling: it cannot even draft, let alone send."""
    toolset = toolset_for(AgentName.MONITOR)
    assert not toolset.can_write
    assert "save_draft" not in toolset.names
    for name in toolset.names:
        assert name.startswith(("read_", "list_", "search_", "get_"))


async def test_the_monitor_cannot_save_a_draft_even_if_asked() -> None:
    """Capability is tool binding, not intent — so the refusal is a PermissionError
    from the runtime, not a policy check somebody can forget to call."""
    ctx = a_context()
    agent = build_agent(FakeLLM(responses=["19 unmarked days."]))
    briefing = await agent.explain_alert(assess_risk(ctx), ctx, audience=(Persona.MANAGER,))

    with pytest.raises(PermissionError, match="save_draft"):
        await agent.runtime.save_draft(
            Draft(
                artifact_type=ArtifactType.PROGRAM_DOCUMENT,
                title="not allowed",
                body=briefing.narrative,
                payload={},
            ),
            briefing.invocation,
        )


def test_a_draft_sink_wired_to_the_monitor_is_still_unreachable() -> None:
    """Belt and braces: even a mis-wired PortBundle cannot give it a write, because
    the toolset gate refuses before the port is consulted."""
    runtime = AgentRuntime(
        agent=AgentName.MONITOR,
        dispatcher=bind(toolset_for(AgentName.MONITOR), PortBundle(drafts=FakeDraftSink())),
        llm=FakeLLM(),
        autonomy=AutonomyLevel.OBSERVE,
    )
    assert not runtime.dispatcher.toolset.can_write


# --- the ceiling and the routing --------------------------------------------


def test_the_monitor_cannot_be_built_above_observe() -> None:
    """§8's ceiling is Alert — level 1. Even Draft is too high, which is why the
    default AgentRuntime autonomy has to be overridden to build one at all."""
    with pytest.raises(AutonomyCeilingError):
        AgentRuntime(
            agent=AgentName.MONITOR,
            dispatcher=bind(toolset_for(AgentName.MONITOR), PortBundle()),
            llm=FakeLLM(),
        )


def test_the_monitor_refuses_another_agents_runtime() -> None:
    with pytest.raises(ValueError, match="monitor runtime"):
        MonitorAgent(
            runtime=AgentRuntime(
                agent=AgentName.COPILOT,
                dispatcher=bind(toolset_for(AgentName.COPILOT), PortBundle()),
                llm=FakeLLM(),
                autonomy=AutonomyLevel.OBSERVE,
            )
        )


async def test_the_briefing_routes_to_the_volume_tier() -> None:
    """§2: a summary is volume work, not frontier."""
    ctx = a_context()
    llm = FakeLLM(responses=["19 unmarked days."])
    agent = build_agent(llm)
    await agent.explain_alert(assess_risk(ctx), ctx, audience=(Persona.MANAGER,))
    assert llm.tasks_called == [LLMTask.SUMMARY]


async def test_the_invocation_records_the_tools_that_were_called() -> None:
    """§11: prompt, tools called, tokens, latency — every invocation."""
    ctx = a_context()
    agent = build_agent(FakeLLM(responses=["19 unmarked days."]))
    briefing = await agent.explain_alert(assess_risk(ctx), ctx, audience=(Persona.MANAGER,))

    assert [call.tool for call in briefing.invocation.tools_called] == [
        "read_program",
        "list_program_tasks",
        "list_internal_contacts",
    ]
    assert briefing.invocation.total_tokens > 0
