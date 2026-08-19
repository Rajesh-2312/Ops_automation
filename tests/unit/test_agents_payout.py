"""The Payout agent. CLAUDE.md §8: "Explain validation failures, draft variance
reasons, run summaries". Ceiling: Draft.

This file exists to make R2 falsifiable.

    R2  "All monetary arithmetic lives in `services/remuneration/engine.py` [...]
         An agent may explain a number. It may never produce one."

An agent that talks about money will produce a number eventually — models round,
approximate and total when a sentence reads better that way. The assertions below
are arranged so that every route by which one could arrive is closed by something
that fails the build rather than by something a reviewer has to notice:

* **Static.** No file under `app/agents/` imports `app.services.remuneration`,
  and `app/agents/payout.py` contains no arithmetic operator at all — no `+`,
  `-`, `*`, `/`, `sum()`, `round()` or `float`. Those two tests are what make the
  module docstring's claims checkable instead of decorative.
* **Behavioural.** A model that states an approximate net, an invented total or a
  figure lifted out of a retrieved SOP passage gets its draft refused, and the
  test asserts the refusal *and* that nothing was saved.
* **Structural.** No release-capable tool is bound, the ceiling cannot be
  exceeded, and the §7 stated reason cannot be satisfied by the agent.
* **§5's asymmetry.** CRT and bCAP are asserted separately and asserted not to be
  each other, because the failure mode is a swap and a swap passes any test that
  only checks one of them.
"""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from app.agents.grounding import (
    UngroundedFigureError,
    collect_grounded_values,
    figures_in,
)
from app.agents.payout import (
    ATTENDANCE_SEMANTICS,
    PayoutAgent,
    PayoutFacts,
    RunLine,
    attendance_semantics,
    tally_run,
)
from app.agents.ports import RetrievedPassage
from app.agents.runtime import AgentRuntime, AutonomyCeilingError
from app.agents.tools import AgentName, PortBundle, ToolEffect, bind, toolset_for
from app.domain.enums import (
    ArtifactState,
    ArtifactType,
    AutonomyLevel,
    LLMTask,
    ProgramType,
    RateBasis,
    ValidationCode,
    ValidationSeverity,
)
from app.domain.money import round_rupees
from app.domain.payout import PayoutInput, PayoutResult
from app.services.remuneration.engine import compute_payout
from app.services.remuneration.validators import ValidationIssue
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeDraftSink,
    FakeLLM,
    FakeProgramPort,
    FakeRetrievalPort,
    a_program,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PAYOUT_MODULE = REPO_ROOT / "app" / "agents" / "payout.py"


class RecordingRetrievalPort:
    """A `RetrievalPort` that remembers what it was asked, and for which corpus.

    `FakeRetrievalPort` returns passages and forgets the question; the corpus and
    the query are both assertions worth making here. §9 permissions corpora
    separately, so a payout question reaching the contracts index would be a real
    defect, and a query the model composed would be one nobody could reproduce.
    """

    def __init__(self) -> None:
        self.queries: list[tuple[str, str]] = []

    async def search_corpus(
        self, corpus: str, query: str, limit: int = 5
    ) -> Sequence[RetrievedPassage]:
        self.queries.append((corpus, query))
        return ()


# --- fixtures: real engine output, so the figures under test are real ---------


def vema_result() -> PayoutResult:
    """CLAUDE.md §6's first regression fixture, computed by the real engine.

    Deliberately not a hand-written `PayoutResult`. The whole question this file
    asks is "can the agent state a figure the engine did not produce?", and
    asking it against invented figures would be asking a different question.

    bCAP, ₹80,000/mo, 26-31 Jul 2026, TA&DA ₹100 -> Net 14,035.
    """
    return compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(80000),
            payable_days=Decimal(6),
            days_in_month=31,
            period_start=dt.date(2026, 7, 26),
            period_end=dt.date(2026, 7, 31),
            ta_da=Decimal(100),
        )
    )


def an_issue(
    code: ValidationCode,
    severity: ValidationSeverity = ValidationSeverity.BLOCKING,
    message: str = "Gate failed.",
    **detail: object,
) -> ValidationIssue:
    """A real `ValidationIssue`, to prove the structural protocol accepts one."""
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        detail={k: str(v) for k, v in detail.items()},
    )


def facts_for(
    *,
    program_type: ProgramType = ProgramType.BCAP,
    issues: tuple[ValidationIssue, ...] = (),
) -> PayoutFacts:
    return PayoutFacts(
        trainer_name="VEMA PRUDHVI SAI",
        trainer_pan="ABCDE1234F",
        program_type=program_type,
        period_start=dt.date(2026, 7, 26),
        period_end=dt.date(2026, 7, 31),
        result=vema_result(),
        issues=issues,
        invoice_number="BCDP/26-27/JUL1",
    )


def build_agent(
    llm: FakeLLM,
    *,
    passages: Sequence[RetrievedPassage] = (),
    program_type: str = "bCAP",
) -> tuple[PayoutAgent, FakeDraftSink]:
    sink = FakeDraftSink()
    ports = PortBundle(
        programs=FakeProgramPort(program=a_program(program_type=program_type)),
        retrieval=FakeRetrievalPort(passages=passages),
        drafts=sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.PAYOUT,
        dispatcher=bind(toolset_for(AgentName.PAYOUT), ports),
        llm=llm,
    )
    return PayoutAgent(runtime=runtime), sink


# --- R2, statically ----------------------------------------------------------


def test_no_agent_imports_the_remuneration_service() -> None:
    """R2 in the import graph. `app/agents/__init__.py` claims this; here it is.

    The engine is the only thing permitted to compute money. An agent module that
    could import `compute_payout` could call it, and then a figure in a draft
    would have been produced inside the agent layer — grounded against itself and
    indistinguishable from one the caller supplied.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "app" / "agents").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "app.services.remuneration"
            ):
                offenders.append(f"{path.name}:{node.lineno} from {node.module}")
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}:{node.lineno} import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("app.services.remuneration")
                )
    assert offenders == [], (
        "CLAUDE.md R2: no agent may reach the remuneration engine or its validators. "
        f"Found: {offenders}"
    )


def test_the_payout_agent_contains_no_arithmetic() -> None:
    """The strongest R2 property available: the module cannot do sums.

    Not one `+`, `-`, `*`, `/`, `//`, `%` or `**`, and no `sum()`, `round()` or
    `float`. Counting is `len()` and formatting is `str()` on a `Decimal` the
    engine produced. `|` is exempt because it is type-union syntax, not
    arithmetic.

    If this fails, read what was added before deleting the test: a helpful
    "gross minus TDS" line or a run total is exactly the defect R2 is written to
    prevent, and it will look reasonable in the diff.
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
    tree = ast.parse(PAYOUT_MODULE.read_text(encoding="utf-8"))

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
    assert operators == [], f"arithmetic in the payout agent (CLAUDE.md R2): {operators}"
    assert calls == [], f"money-shaped builtins in the payout agent (CLAUDE.md R2): {calls}"


# --- R3: no release capability ------------------------------------------------


def test_the_payout_toolset_exposes_no_release_capable_tool() -> None:
    """§12: "assert no agent toolset exposes a release-capable tool"."""
    toolset = toolset_for(AgentName.PAYOUT)
    assert toolset.effects <= {ToolEffect.READ, ToolEffect.SAVE_DRAFT}
    for name in toolset.names:
        assert name == "save_draft" or name.startswith(("read_", "list_", "get_", "search_"))
    for forbidden in ("send_email", "send_whatsapp", "post_message", "mark_released", "release"):
        assert forbidden not in toolset.names


def test_the_payout_toolset_returns_no_amount() -> None:
    """The catalogue's own note, asserted: no tool here hands back a rupee.

    `read_program` returns a `ProgramSnapshot`, which carries no commercials by
    construction; `search_corpus` returns cited prose. So the only route a figure
    has into this agent is `PayoutFacts`, supplied by the caller that ran the
    engine.
    """
    assert set(toolset_for(AgentName.PAYOUT).names) == {
        "read_program",
        "search_corpus",
        "save_draft",
    }


def test_payout_cannot_be_built_above_the_draft_ceiling() -> None:
    with pytest.raises(AutonomyCeilingError):
        AgentRuntime(
            agent=AgentName.PAYOUT,
            dispatcher=bind(toolset_for(AgentName.PAYOUT), PortBundle()),
            llm=FakeLLM(),
            autonomy=AutonomyLevel.ACT,
        )


def test_the_payout_agent_refuses_a_runtime_for_another_agent() -> None:
    with pytest.raises(ValueError, match="payout runtime"):
        PayoutAgent(
            runtime=AgentRuntime(
                agent=AgentName.SOURCING,
                dispatcher=bind(toolset_for(AgentName.SOURCING), PortBundle()),
                llm=FakeLLM(),
            )
        )


# --- §5: the asymmetry, and that it is not swapped ---------------------------


def test_crt_counts_payable_days_up_and_blocks_on_incomplete_attendance() -> None:
    """CRT: no `P`, no money — so an unmarked day underpays and must BLOCK (§5, §7)."""
    crt = attendance_semantics(ProgramType.CRT)
    assert crt.rate_basis is RateBasis.PER_DAY
    assert "UP" in crt.payable_days_direction
    assert "UNDERPAYS" in crt.unmarked_day_effect
    assert crt.completeness_severity is ValidationSeverity.BLOCKING


def test_bcap_counts_payable_days_down_and_only_warns() -> None:
    """bCAP: the retainer stands, so an unmarked day pays — a WARNING (§5, §7)."""
    bcap = attendance_semantics(ProgramType.BCAP)
    assert bcap.rate_basis is RateBasis.PER_MONTH
    assert "DOWN" in bcap.payable_days_direction
    assert "PAYS" in bcap.unmarked_day_effect
    assert bcap.completeness_severity is ValidationSeverity.WARNING


def test_the_two_program_types_are_not_each_other() -> None:
    """The failure mode is a swap, and a swap passes any one-sided assertion.

    Asserted as inequality on every field that differs, so a copy-paste that
    leaves both rows describing bCAP fails here rather than in a payout dispute.
    """
    crt = ATTENDANCE_SEMANTICS[ProgramType.CRT]
    bcap = ATTENDANCE_SEMANTICS[ProgramType.BCAP]
    assert crt.rate_basis is not bcap.rate_basis
    assert crt.payable_days_direction != bcap.payable_days_direction
    assert crt.unmarked_day_effect != bcap.unmarked_day_effect
    assert crt.completeness_severity is not bcap.completeness_severity


def test_an_unmapped_program_type_raises_rather_than_defaulting() -> None:
    """No default row. Inheriting bCAP's semantics is how a CRT trainer gets
    prorated for days nobody marked."""
    with pytest.raises(KeyError):
        attendance_semantics("per_hour")  # type: ignore[arg-type]


async def test_the_explanation_is_told_the_semantics_rather_than_asked_to_reason() -> None:
    """§5's answer reaches the model as data, in the programme's own row."""
    llm = FakeLLM(responses=["Attendance is incomplete for 2 day(s)."])
    agent, _ = build_agent(llm, program_type="CRT")
    facts = facts_for(
        program_type=ProgramType.CRT,
        issues=(
            an_issue(
                ValidationCode.ATTENDANCE_INCOMPLETE,
                ValidationSeverity.BLOCKING,
                "Attendance incomplete: 2 unmarked day(s).",
                unmarked_days=2,
            ),
        ),
    )
    await agent.explain_validation_failures(PROGRAM_ID, facts)

    prompt = str(llm.calls[0]["user"])
    assert "counted UP from P marks" in prompt
    assert "UNDERPAYS" in prompt
    assert "counted DOWN" not in prompt


# --- R1/R2: every number in the text came from the engine ---------------------


async def test_the_engines_figures_may_be_quoted_in_the_display_form() -> None:
    """§6's VEMA fixture, quoted as a human would write it.

    Earned 15,484 · gross 15,584 · TDS 1,548 · net 14,035 — the row in CLAUDE.md
    §6, which is the whole-rupee display form (§11) of full-precision engine
    values. `assert_grounded` compares by numeric value, so `15,484` and `15484`
    are the same claim and the model may format as it likes.
    """
    body = (
        "Earned is 15,484 against 6 payable days of 31. With TA&DA of 100 the gross is "
        "15,584, TDS is 1,548 and the net pay is 14,035."
    )
    llm = FakeLLM(responses=[body])
    agent, sink = build_agent(llm)
    outcome = await agent.explain_validation_failures(PROGRAM_ID, facts_for())

    assert outcome.saved.state is ArtifactState.DRAFT
    assert sink.saved[0][0].body == body


async def test_the_full_precision_form_may_also_be_quoted() -> None:
    """R6 keeps full precision through every intermediate, and a dispute is
    walked at that precision. Both forms are in the grounded set."""
    exact = str(vema_result().earned)
    agent, _ = build_agent(FakeLLM(responses=[f"Earned to full precision is {exact}."]))
    outcome = await agent.explain_validation_failures(PROGRAM_ID, facts_for())
    assert outcome.saved.state is ArtifactState.DRAFT


def test_the_display_form_reproduces_the_claude_md_fixture_row() -> None:
    """The display block is `round_rupees` + `format_indian` from
    `app.domain.money` — the codebase's own helpers, not a formatter invented
    here — and on §6's VEMA row it lands on §6's own figures."""
    payout = facts_for().as_payload()["payout"]
    assert isinstance(payout, dict)
    assert payout["display"] == {
        "rate_per_day": "2,581",
        "earned": "15,484",
        "reimbursements": "100",
        "gross": "15,584",
        "tds": "1,548",
        "deductions": "0",
        "net": "14,035",
    }


async def test_an_approximated_net_is_refused() -> None:
    """The exact failure `app.agents.grounding` was written for.

    "approximately 15,500" where the engine computed 15,484 is a produced number,
    and the reader cannot tell which of the two they are looking at. The draft is
    refused, not corrected, and nothing is saved.
    """
    agent, sink = build_agent(
        FakeLLM(responses=["The net pay is approximately 15,500 for the period."])
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.explain_validation_failures(PROGRAM_ID, facts_for())

    assert exc.value.context == "payout.explain_validation_failures"
    assert not sink.saved


async def test_a_recomputed_figure_is_refused_even_when_the_arithmetic_is_right() -> None:
    """Gross minus TDS is 14,035.483 — correct, and still refused.

    R6 permits one rounding and the engine has already done it. A model that
    reaches an arithmetically defensible intermediate has still produced a number,
    and the next one it produces will not be defensible.
    """
    agent, sink = build_agent(FakeLLM(responses=["Net before rounding works out at 14035.483."]))
    with pytest.raises(UngroundedFigureError):
        await agent.explain_validation_failures(PROGRAM_ID, facts_for())
    assert not sink.saved


async def test_every_figure_in_the_text_is_checked_against_the_structured_input() -> None:
    """§12, literally: compare every number in generated text against the input.

    Asserted here as a property rather than as one example — the grounded set is
    reconstructed from the draft's own `grounded_in`, and every digit run in the
    body is required to be in it.
    """
    body = "Net pay is 14,035 for 6 payable days in a 31-day month; TDS came to 1,548."
    agent, sink = build_agent(FakeLLM(responses=[body]))
    await agent.explain_validation_failures(PROGRAM_ID, facts_for())

    draft, _ = sink.saved[0]
    allowed = collect_grounded_values(draft.grounded_in)
    written = figures_in(draft.body)
    assert written, "the fixture must actually contain figures for this to assert anything"
    assert [value for _, value in written if value not in allowed] == []


def test_the_exact_payload_covers_every_engine_field() -> None:
    """Driven off `PayoutResult.NUMERIC_FIELDS`, so a new engine field is not
    silently dropped out of every explanation."""
    payout = facts_for().as_payload()["payout"]
    assert isinstance(payout, dict)
    exact = payout["exact"]
    assert isinstance(exact, dict)
    for field in PayoutResult.NUMERIC_FIELDS:
        assert field in exact, f"{field} missing from the explained payload"
        assert isinstance(exact[field], str), f"{field} must be a string, not a float (R7)"


def test_no_amount_reaches_a_prompt_as_a_float() -> None:
    """R7 at the serialisation boundary.

    `json.dumps` renders a float and a `Decimal` differently and only one of them
    is the figure the engine computed. Every monetary value on the payload is a
    string, so there is nothing for JSON to reformat.
    """
    payout = facts_for().as_payload()["payout"]
    assert isinstance(payout, dict)
    floats = [
        f"{block}.{key}"
        for block in ("exact", "display")
        for key, value in payout[block].items()  # type: ignore[union-attr]
        if isinstance(value, float)
    ]
    assert floats == []


# --- §9: a figure from a policy passage is not grounded ----------------------


async def test_a_figure_lifted_from_a_retrieved_sop_passage_is_refused() -> None:
    """§9: RAG supplies policy and context, never structured facts.

    The passage TEXT reaches the prompt but not the grounded set, so a TDS rate
    quoted out of an SOP page — the number that must come from the engine — is
    refused. This is stricter than `intake.py`, deliberately; see the module
    docstring.
    """
    passage = RetrievedPassage(
        document_title="Payout SOP",
        section="Deductions",
        text="TDS is deducted at 12 percent for unregistered vendors.",
    )
    agent, sink = build_agent(
        FakeLLM(responses=["TDS was applied at 12 percent per the payout SOP."]),
        passages=(passage,),
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.explain_validation_failures(PROGRAM_ID, facts_for())

    assert any(v.value == Decimal(12) for v in exc.value.violations)
    assert not sink.saved


async def test_the_passage_text_reaches_the_prompt_but_not_the_grounded_set() -> None:
    """The two halves are visibly separated (§9) and only one of them grounds."""
    passage = RetrievedPassage(
        document_title="Payout SOP",
        section="Work orders",
        text="A signed work order must be on file before any cycle is submitted.",
    )
    llm = FakeLLM(responses=["A signed work order is required before submission."])
    agent, sink = build_agent(llm, passages=(passage,))
    await agent.explain_validation_failures(
        PROGRAM_ID, facts_for(issues=(an_issue(ValidationCode.WORK_ORDER_MISSING),))
    )

    prompt = str(llm.calls[0]["user"])
    assert "A signed work order must be on file" in prompt
    draft, _ = sink.saved[0]
    assert draft.grounded_in["sources"] == [{"document": "Payout SOP", "section": "Work orders"}]
    assert "must be on file" not in str(draft.grounded_in)


# --- §7: the gates it explains are the real ones ------------------------------


async def test_blocking_and_warning_gates_are_reported_separately() -> None:
    """§7 splits blocking from warning, and so does the explanation.

    A warning presented as a blocker sends a Manager chasing a fix that was never
    required; a blocker presented as a warning ships an underpayment.
    """
    facts = facts_for(
        issues=(
            an_issue(ValidationCode.WORK_ORDER_MISSING),
            an_issue(
                ValidationCode.NET_DEVIATION_FROM_TRAILING_AVERAGE,
                ValidationSeverity.WARNING,
                "Net pay deviates from the trailing average.",
            ),
        )
    )
    agent, sink = build_agent(FakeLLM(responses=["No signed work order is on file."]))
    await agent.explain_validation_failures(PROGRAM_ID, facts)

    draft, _ = sink.saved[0]
    assert draft.payload["blocking"] == [
        {
            "code": "work_order_missing",
            "severity": "blocking",
            "message": "Gate failed.",
            "detail": {},
        }
    ]
    warnings = draft.payload["warnings"]
    assert isinstance(warnings, list) and len(warnings) == 1
    assert draft.payload["is_blocked"] is True
    assert any("BLOCKED" in flag for flag in draft.flags)


async def test_a_clean_cycle_is_explained_as_clean() -> None:
    agent, sink = build_agent(FakeLLM(responses=["Every gate passed."]))
    await agent.explain_validation_failures(PROGRAM_ID, facts_for())
    draft, _ = sink.saved[0]
    assert draft.payload["is_blocked"] is False
    assert any("no §7 gate failed" in flag for flag in draft.flags)


async def test_the_policy_query_names_the_gates_that_actually_failed() -> None:
    """A deterministic query built in Python from the failed gate codes.

    A retrieval query the model composes is a query nobody can reproduce when the
    answer turns out to be wrong, so the query is derived from `ValidationCode`
    values and the corpus is pinned to SOP (§9's corpora are separately
    permissioned; a payout question does not go to the contracts index).
    """
    port = RecordingRetrievalPort()
    sink = FakeDraftSink()
    runtime = AgentRuntime(
        agent=AgentName.PAYOUT,
        dispatcher=bind(
            toolset_for(AgentName.PAYOUT),
            PortBundle(programs=FakeProgramPort(program=a_program()), retrieval=port, drafts=sink),
        ),
        llm=FakeLLM(responses=["Explained."]),
    )
    facts = facts_for(
        issues=(
            an_issue(ValidationCode.ZOHO_ACCOUNT_MISSING),
            an_issue(ValidationCode.PAN_INVALID),
        )
    )
    await PayoutAgent(runtime=runtime).explain_validation_failures(PROGRAM_ID, facts)

    assert port.queries == [
        ("sop", "payout validation gate policy: pan_invalid, zoho_account_missing")
    ]


async def test_a_programme_type_mismatch_is_flagged() -> None:
    """The tracker says CRT, the payout was computed as bCAP.

    §5 makes payable-day counting depend on the type, so the figures are
    internally consistent and wrong — the hardest kind of wrong to spot in a
    sheet, and the reason this is a flag rather than a silent detail.
    """
    agent, sink = build_agent(FakeLLM(responses=["Explained."]), program_type="CRT")
    await agent.explain_validation_failures(PROGRAM_ID, facts_for(program_type=ProgramType.BCAP))
    draft, _ = sink.saved[0]
    assert any("programme type mismatch" in flag for flag in draft.flags)


# --- the variance reason cannot become a stated reason ------------------------


async def test_a_variance_reason_is_proposed_wording_and_never_a_stated_reason() -> None:
    """§7's stated reason is a gate, and an agent that could satisfy it would be
    an agent that unblocks a payout.

    The draft carries the wording, says `stated: false`, and stays in DRAFT.
    Nothing in this agent's toolset could attach it to a run.
    """
    facts = facts_for(
        issues=(
            an_issue(
                ValidationCode.NET_DEVIATION_FROM_TRAILING_AVERAGE,
                ValidationSeverity.WARNING,
                "Net pay deviates from the trailing average by more than 20%.",
            ),
        )
    )
    agent, sink = build_agent(
        FakeLLM(responses=["The period covers 6 days rather than a full month."])
    )
    outcome = await agent.draft_variance_reason(
        PROGRAM_ID, facts, ValidationCode.NET_DEVIATION_FROM_TRAILING_AVERAGE
    )

    draft, _ = sink.saved[0]
    assert outcome.saved.state is ArtifactState.DRAFT
    assert draft.payload["stated"] is False
    assert any("PROPOSED WORDING ONLY" in flag for flag in draft.flags)


async def test_a_variance_reason_for_a_warning_that_did_not_fire_is_refused() -> None:
    """R1 at its plainest: an agent may not explain a gate outcome that is not in
    the record."""
    agent, sink = build_agent(FakeLLM(responses=["Unused."]))
    with pytest.raises(ValueError, match="No warning"):
        await agent.draft_variance_reason(
            PROGRAM_ID, facts_for(), ValidationCode.NET_DEVIATION_FROM_TRAILING_AVERAGE
        )
    assert not sink.saved


async def test_a_variance_reason_cannot_be_drafted_against_a_blocking_gate() -> None:
    """§7 gives no reason path to a blocker. A blocking gate is fixed, not
    explained away, and `can_submit()` ignores stated reasons for blockers."""
    facts = facts_for(issues=(an_issue(ValidationCode.NET_PAY_NOT_POSITIVE),))
    agent, _ = build_agent(FakeLLM(responses=["Unused."]))
    with pytest.raises(ValueError, match="No warning"):
        await agent.draft_variance_reason(PROGRAM_ID, facts, ValidationCode.NET_PAY_NOT_POSITIVE)


# --- run summaries: counts, never a total ------------------------------------


def a_line(name: str, *, net: str | None, blocking: tuple[ValidationCode, ...] = ()) -> RunLine:
    return RunLine(
        trainer_name=name,
        trainer_pan="ABCDE1234F",
        program_type=ProgramType.BCAP,
        net=Decimal(net) if net is not None else None,
        blocking_codes=blocking,
    )


def test_the_tally_counts_and_does_not_total() -> None:
    tally = tally_run(
        (
            a_line("A", net="14035"),
            a_line("B", net="58500", blocking=(ValidationCode.PAN_INVALID,)),
            a_line("C", net=None),
        )
    )
    assert tally.trainer_count == 3
    assert tally.blocked_count == 1
    assert tally.uncomputed_count == 1
    assert tally.blocking_code_counts == {"pan_invalid": 1}
    assert tally.as_payload()["total_net"] is None


async def test_a_run_total_the_model_invented_is_refused() -> None:
    """14,035 + 58,500 = 72,535, which nothing computed.

    This is the assertion that matters most in the file. A run summary is where
    "what did this month cost?" gets asked, and a plausible total is the single
    most likely fabrication in the whole agent layer.
    """
    lines = (a_line("VEMA", net="14035"), a_line("Bushily", net="58500"))
    agent, sink = build_agent(FakeLLM(responses=["The run totals 72,535 across 2 trainers."]))
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_run_summary(PROGRAM_ID, lines, period_label="2026-07")

    assert any(v.value == Decimal(72535) for v in exc.value.violations)
    assert not sink.saved


async def test_a_run_summary_may_quote_each_engine_written_net() -> None:
    lines = (a_line("VEMA", net="14035"), a_line("Bushily", net="58500"))
    body = "2 trainer-months: VEMA at 14,035 and Bushily at 58,500, both clear of blockers."
    agent, sink = build_agent(FakeLLM(responses=[body]))
    outcome = await agent.draft_run_summary(PROGRAM_ID, lines, period_label="2026-07")

    assert outcome.saved.state is ArtifactState.DRAFT
    assert sink.saved[0][0].payload["tally"]["total_net"] is None  # type: ignore[index]


async def test_the_run_summary_flags_that_no_total_was_stated() -> None:
    agent, sink = build_agent(FakeLLM(responses=["Nothing to report."]))
    await agent.draft_run_summary(PROGRAM_ID, (), period_label="2026-07")
    draft, _ = sink.saved[0]
    assert any("no run total is stated" in flag for flag in draft.flags)
    assert any("the run is empty" in flag for flag in draft.flags)


# --- §2: routing by task, and §11: the invocation record ----------------------


async def test_explanations_and_run_summaries_route_to_the_volume_tier() -> None:
    """§2: drafting and summaries are volume work. Payout produces neither
    document extractions nor governance reports, so nothing here is frontier."""
    llm = FakeLLM(responses=["Explained.", "Summarised."])
    agent, _ = build_agent(llm)
    await agent.explain_validation_failures(PROGRAM_ID, facts_for())
    await agent.draft_run_summary(PROGRAM_ID, (), period_label="2026-07")
    assert llm.tasks_called == [LLMTask.DRAFTING, LLMTask.SUMMARY]


async def test_every_invocation_is_recorded_with_its_tool_calls() -> None:
    """§11: prompt, tools called, tokens, latency — for every invocation."""
    agent, _ = build_agent(FakeLLM(responses=["Explained."]))
    outcome = await agent.explain_validation_failures(PROGRAM_ID, facts_for())

    invocation = outcome.invocation
    assert invocation.agent is AgentName.PAYOUT
    assert invocation.prompt_chars > 0
    assert invocation.total_tokens > 0
    assert [call.tool for call in invocation.tools_called] == ["read_program", "search_corpus"]
    assert all(call.effect is ToolEffect.READ for call in invocation.tools_called)


async def test_saving_a_draft_writes_an_audit_row_naming_the_agent() -> None:
    """§11: every state transition writes an `AuditEvent`."""
    agent, sink = build_agent(FakeLLM(responses=["Explained."]))
    await agent.explain_validation_failures(PROGRAM_ID, facts_for())

    _, event = sink.saved[0]
    assert event.action == "agent.draft_saved"
    assert event.before is None
    assert event.after is not None
    assert event.after["agent"] == "payout"
    assert event.after["autonomy"] == AutonomyLevel.DRAFT.value


async def test_a_payout_draft_is_not_filed_as_a_remuneration_sheet() -> None:
    """An explanation is a note about a sheet, not a sheet.

    `ArtifactType`'s value doubles as the audit row's `entity_table` (§11), so
    typing this as `REMUNERATION_SHEET` would file agent prose against the table
    Finance reconciles from.
    """
    agent, sink = build_agent(FakeLLM(responses=["Explained."]))
    await agent.explain_validation_failures(PROGRAM_ID, facts_for())
    draft, event = sink.saved[0]
    assert draft.artifact_type is not ArtifactType.REMUNERATION_SHEET
    assert event.entity_table == draft.artifact_type.value


# --- the protocol accepts the real validators' type --------------------------


def test_a_real_validation_issue_satisfies_the_structural_protocol() -> None:
    """`PayoutFacts` is typed against a `Protocol` so that nothing under
    `app/agents/` imports `app/services/`. This asserts the seam actually fits."""
    issue = an_issue(ValidationCode.IFSC_INVALID, message="IFSC missing.", ifsc_length=0)
    facts = facts_for(issues=(issue,))
    assert facts.blocking == (issue,)
    payload = facts.as_payload()["blocking"]
    assert payload == [
        {
            "code": "ifsc_invalid",
            "severity": "blocking",
            "message": "IFSC missing.",
            "detail": {"ifsc_length": "0"},
        }
    ]


def test_the_fixture_this_file_reasons_about_is_the_one_in_claude_md() -> None:
    """§6's VEMA row, to the rupee. If this breaks, the assertions above are
    reasoning about figures that are not the engine's.

    `net` is the only rounded value in the chain (R6); `gross` still carries full
    precision, which is exactly why the payload needs a display form as well as
    an exact one.
    """
    result = vema_result()
    assert result.net == Decimal(14035)
    assert result.gross == Decimal("15583.87096774193548387096774")
    assert round_rupees(result.earned) == Decimal(15484)
    assert round_rupees(result.tds) == Decimal(1548)
