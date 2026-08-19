"""The Intake agent. CLAUDE.md §8 ("Parse MoU/PO/mail -> structured Program
draft, flag unusual clauses"), under R1, R2 and the Draft ceiling.

The LLM is mocked throughout — no live OpenRouter call. That is not only about
cost: the assertions that matter here are about what happens when the model gets
it *wrong*, and a real model cannot be asked to hallucinate on cue.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.agents.grounding import UngroundedFigureError
from app.agents.intake import INTAKE_FIELDS, IntakeAgent
from app.agents.parsing import MalformedModelOutputError
from app.agents.ports import ProgramSnapshot, RetrievedPassage
from app.agents.runtime import AgentRuntime, AutonomyCeilingError
from app.agents.tools import AgentName, PortBundle, bind, toolset_for
from app.domain.enums import (
    ArtifactState,
    AutonomyLevel,
    LLMTask,
    ModelTier,
    Persona,
    ProgramStage,
)
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeDraftSink,
    FakeLLM,
    FakeProgramPort,
    FakeRetrievalPort,
    a_program,
)

MOU = """
MEMORANDUM OF UNDERSTANDING between byteXL and Malineni Lakshmaiah College.
Program type: bCAP. 3 batches covering 180 trainees.
Term: starts 2026-07-01 and ends 2026-12-31.
Contract value: INR 1200000.
Payment terms: 30 days from invoice.
Notice period: 60 days.
"""

EXTRACTION = json.dumps(
    {
        "college_name": "Malineni Lakshmaiah College",
        "program_type": "bCAP",
        "batch_count": 3,
        "trainee_count": 180,
        "starts_on": "2026-07-01",
        "ends_on": "2026-12-31",
        "contract_value": 1200000,
        "payment_terms": "30 days from invoice",
        "notice_period": "60 days",
        "deliverables": None,
        "unusual_clauses": ["Notice period of 60 days is longer than the usual 30 days"],
    }
)

SUMMARY = (
    "Check on the paper copy: contract value 1200000 and the notice period of 60 days. "
    "The record covers 3 batches and 180 trainees. "
    "The term runs 2026-07-01 to 2026-12-31. "
    "Deliverables are not stated."
)


def build_agent(
    llm: FakeLLM,
    *,
    sink: FakeDraftSink | None = None,
    programs: FakeProgramPort | None = None,
    autonomy: AutonomyLevel = AutonomyLevel.DRAFT,
) -> tuple[IntakeAgent, FakeDraftSink]:
    draft_sink = sink or FakeDraftSink()
    ports = PortBundle(
        programs=programs or FakeProgramPort(program=a_program()),
        retrieval=FakeRetrievalPort(
            passages=(
                RetrievedPassage(
                    document_title="Standard MoU template",
                    section="Clause 7 — Notice",
                    text="Notice period is customarily thirty days.",
                ),
            )
        ),
        drafts=draft_sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.INTAKE,
        dispatcher=bind(toolset_for(AgentName.INTAKE), ports),
        llm=llm,
        autonomy=autonomy,
    )
    return IntakeAgent(runtime=runtime), draft_sink


# --- the happy path ----------------------------------------------------------


async def test_extracts_the_declared_fields() -> None:
    agent, _ = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    result = await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")

    assert set(result.extracted) == set(INTAKE_FIELDS)
    assert result.extracted["contract_value"] == 1200000
    assert result.extracted["batch_count"] == 3
    # A field the document does not state stays null. §14 says carry open
    # questions; the same discipline applies to an unstated contract field.
    assert result.extracted["deliverables"] is None


async def test_unusual_clauses_are_flagged_for_the_reviewer() -> None:
    agent, _ = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    result = await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert result.unusual_clauses == ("Notice period of 60 days is longer than the usual 30 days",)


async def test_the_output_is_a_draft_and_only_a_draft() -> None:
    """R3 and R4: the agent's one write produces DRAFT and stops there."""
    agent, sink = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    result = await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")

    assert result.outcome.saved.state is ArtifactState.DRAFT
    assert result.outcome.saved.version == 1
    assert len(sink.saved) == 1


async def test_saving_a_draft_writes_an_audit_event() -> None:
    """§11: every state transition writes an AuditEvent — actor, action, before, after."""
    agent, sink = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    await agent.draft_program(
        document_text=MOU,
        source_label="MoU — Malineni",
        actor_id=None,
        actor_persona=Persona.MANAGER,
    )
    _, event = sink.saved[0]
    assert event.action == "agent.draft_saved"
    assert event.before is None  # a draft has no prior state; it begins to exist
    assert event.after is not None
    assert event.after["agent"] == "intake"
    assert event.after["autonomy"] == AutonomyLevel.DRAFT.value


async def test_the_audit_row_does_not_copy_the_draft_body() -> None:
    """An audit table is not a content store — see `_draft_snapshot`."""
    agent, sink = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    _, event = sink.saved[0]
    assert event.after is not None
    assert event.after["body_chars"] == len(SUMMARY)
    assert SUMMARY not in json.dumps(event.after)


# --- §2 routing --------------------------------------------------------------


async def test_extraction_is_frontier_and_prose_is_volume() -> None:
    """§2: "route by task, not by default". Extraction is the frontier case."""
    llm = FakeLLM(responses=[EXTRACTION, SUMMARY])
    agent, _ = build_agent(llm)
    await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert llm.tasks_called == [LLMTask.EXTRACTION, LLMTask.DRAFTING]


async def test_no_model_id_is_chosen_by_the_agent() -> None:
    """The agent names a task; the gateway resolves the model (§2)."""
    llm = FakeLLM(responses=[EXTRACTION, SUMMARY])
    agent, _ = build_agent(llm)
    result = await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert result.outcome.invocation.model == f"fake/{ModelTier.VOLUME.value}"


# --- R1: the agent cannot state a figure it was not given --------------------


async def test_an_extracted_figure_absent_from_the_document_is_refused() -> None:
    """The check most systems skip: a hallucinated *field value*.

    A fabricated commercial term arrives looking structured, which is exactly why
    a reviewer trusts it more than prose. R1 catches it against the source
    document, which is the system of record for an unparsed contract.
    """
    invented = json.loads(EXTRACTION)
    invented["contract_value"] = 5000000  # appears nowhere in the MoU
    agent, sink = build_agent(FakeLLM(responses=[json.dumps(invented), SUMMARY]))

    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert exc.value.context == "intake.extract"
    assert not sink.saved, "nothing may be persisted when grounding fails"


async def test_an_invented_figure_in_the_summary_is_refused() -> None:
    """R2's shape: the model may quote 3 and 180, never their arithmetic."""
    agent, sink = build_agent(
        FakeLLM(responses=[EXTRACTION, "3 batches of 180 trainees is 540 trainee-places."])
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert exc.value.context == "intake.summary"
    assert not sink.saved


async def test_a_clause_flag_quoting_an_absent_figure_is_refused() -> None:
    """Flags are model-authored prose too, and R1 does not exempt them.

    Caught at `intake.extract`, because the clause list is part of the extraction
    response and that whole response is grounded against the source document.
    """
    payload = json.loads(EXTRACTION)
    payload["unusual_clauses"] = ["Penalty of 250000 on early termination"]
    agent, _ = build_agent(FakeLLM(responses=[json.dumps(payload), SUMMARY]))
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert exc.value.context == "intake.extract"


async def test_a_clause_may_not_quote_a_figure_only_the_context_supplied() -> None:
    """The narrow case the second grounding net exists for.

    The extraction is grounded against the document *and* the existing program
    record, because confirming a value against the record is legitimate. A clause
    *flag*, though, describes the contract, so it is grounded against the document
    alone. A figure that came only from the record slips the first net and is
    caught by the second.
    """
    stale = ProgramSnapshot(
        program_id=PROGRAM_ID,
        college_name="Malineni Lakshmaiah",
        stage=ProgramStage.ACQUISITION_SETUP,
        program_type="bCAP",
        starts_on=dt.date(2025, 3, 15),
        ends_on=dt.date(2025, 9, 30),
    )
    payload = json.loads(EXTRACTION)
    payload["unusual_clauses"] = ["Start date differs from the recorded 2025-03-15"]
    agent, sink = build_agent(
        FakeLLM(responses=[json.dumps(payload), SUMMARY]),
        programs=FakeProgramPort(program=stale),
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_program(
            document_text=MOU, source_label="MoU — Malineni", program_id=PROGRAM_ID
        )
    assert exc.value.context == "intake.clauses"
    assert not sink.saved


async def test_every_figure_in_the_body_traces_to_the_structured_input() -> None:
    """§12, stated directly: compare every number in generated text to the input."""
    from app.agents.grounding import collect_grounded_values, figures_in

    agent, _ = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    result = await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")

    allowed = collect_grounded_values(result.outcome.draft.grounded_in)
    assert allowed, "the fixture would be vacuous with an empty grounding set"
    for _, value in figures_in(result.outcome.draft.body):
        assert value in allowed


# --- malformed output --------------------------------------------------------


async def test_a_non_json_extraction_is_refused() -> None:
    agent, sink = build_agent(FakeLLM(responses=["I could not read that contract.", SUMMARY]))
    with pytest.raises(MalformedModelOutputError):
        await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert not sink.saved


async def test_a_fenced_json_block_is_read() -> None:
    """Models fence their JSON. That is a formatting habit, not a failure."""
    agent, _ = build_agent(FakeLLM(responses=[f"```json\n{EXTRACTION}\n```", SUMMARY]))
    result = await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")
    assert result.extracted["program_type"] == "bCAP"


async def test_an_unrequested_field_becomes_a_flag_not_a_record_field() -> None:
    """Nothing downstream knows what to do with a key outside INTAKE_FIELDS."""
    payload = json.loads(EXTRACTION)
    payload["renewal_option"] = "annual"
    agent, _ = build_agent(FakeLLM(responses=[json.dumps(payload), SUMMARY]))
    result = await agent.draft_program(document_text=MOU, source_label="MoU — Malineni")

    assert "renewal_option" not in result.extracted
    assert any("renewal_option" in flag for flag in result.unusual_clauses)


# --- §8 ceiling and §11 logging ----------------------------------------------


def test_intake_cannot_be_built_above_the_draft_ceiling() -> None:
    """§8 puts Intake at Draft. A graph above its ceiling must fail to build."""
    with pytest.raises(AutonomyCeilingError) as exc:
        AgentRuntime(
            agent=AgentName.INTAKE,
            dispatcher=bind(toolset_for(AgentName.INTAKE), PortBundle()),
            llm=FakeLLM(),
            autonomy=AutonomyLevel.ACT_WITH_APPROVAL,
        )
    assert "§8" in str(exc.value)


def test_a_runtime_cannot_borrow_another_agents_toolset() -> None:
    """Toolsets are per-agent bindings (R3), not a shared pool of capability."""
    with pytest.raises(ValueError, match="toolset"):
        AgentRuntime(
            agent=AgentName.INTAKE,
            dispatcher=bind(toolset_for(AgentName.SOURCING), PortBundle()),
            llm=FakeLLM(),
        )


async def test_the_invocation_record_carries_tokens_tools_and_latency() -> None:
    """§11: "Log agent I/O — prompt, tools called, tokens, latency"."""
    agent, _ = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    result = await agent.draft_program(
        document_text=MOU, source_label="MoU — Malineni", program_id=PROGRAM_ID
    )
    invocation = result.outcome.invocation

    assert invocation.agent is AgentName.INTAKE
    assert invocation.prompt_chars > 0
    assert invocation.total_tokens == 160
    assert invocation.latency_ms == 7
    assert [call.tool for call in invocation.tools_called] == [
        "read_program",
        "list_program_documents",
        "search_corpus",
    ]


async def test_tool_results_are_summarised_not_logged_verbatim() -> None:
    """Tool results carry PII; the §11 record keeps shapes, not contents."""
    agent, _ = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    result = await agent.draft_program(
        document_text=MOU, source_label="MoU — Malineni", program_id=PROGRAM_ID
    )
    summaries = [call.result_summary for call in result.outcome.invocation.tools_called]
    assert "Malineni" not in " ".join(summaries)


async def test_intake_runs_without_an_existing_program() -> None:
    """Intake usually runs *before* a program exists; it must not need one."""
    agent, _ = build_agent(FakeLLM(responses=[EXTRACTION, SUMMARY]))
    result = await agent.draft_program(document_text=MOU, source_label="PO — new college")
    assert result.outcome.invocation.tools_called == ()
    assert result.outcome.draft.program_id is None
