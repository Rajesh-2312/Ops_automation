"""The eval set against the structured-fact gate. Offline, deterministic, free.

§9: "Structured facts (dates, amounts, counts) are **never** retrieved from RAG.
Query the database."

`app/rag/guards.py` implements that as two regexes and one exemption, evaluated
before retrieval. This module scores them in both directions, because both
directions are defects:

  * a question about a stored figure that reaches retrieval is R1 broken — the
    model gets a context window with a plausible number in it
  * a policy question refused as a figure is §13 broken — Phase 3's gate is
    "trusted by Managers for policy lookups", and a tool that refuses "how long
    is a work order valid for?" is not trusted, it is worked around

Cases carrying a `defect` id are `xfail(strict=True)`: they are the current
failures, they are listed in `docs/rag-findings.md` under that id, and fixing
one turns this suite red until its marker is deleted. That is the point — a
known-defect list that does not notice being fixed rots within a month.
"""

from __future__ import annotations

import pytest

from app.rag.guards import is_structured_fact_question, structured_fact_refusal
from tests.rag_eval.eval_set import (
    ALL_CASES,
    NO_SOURCE_CASES,
    POLICY_CASES,
    STRUCTURED_FACT_CASES,
    EvalCase,
    Expected,
)


def _case_param(case: EvalCase) -> object:
    marks = (
        [pytest.mark.xfail(strict=True, reason=f"known defect {case.defect}")]
        if case.defect
        else []
    )
    return pytest.param(case, id=case.id, marks=marks)


@pytest.mark.parametrize("case", [_case_param(c) for c in ALL_CASES])
def test_structured_fact_gate_matches_the_specification(case: EvalCase) -> None:
    """One case, one verdict: does §9 send this question to SQL or to the corpus."""
    assert (
        is_structured_fact_question(case.question) is case.expects_structured_fact_refusal
    ), f"{case.id}: expected {case.expected.value} — {case.why}"


def test_the_refusal_names_the_system_of_record() -> None:
    """A refusal that only says "no" trains people to stop asking (§13)."""
    message = structured_fact_refusal("How many payable days?").message
    assert "tracksheet" in message
    assert "policy" in message.lower()


def test_defect_inventory_is_exact() -> None:
    """The eval set's `defect` ids must match what the code actually does.

    Without this, a regex edit that fixes three cases and breaks two elsewhere
    still leaves every marker in place and the findings document silently wrong.
    """
    observed_failures = {
        case.id
        for case in ALL_CASES
        if is_structured_fact_question(case.question) is not case.expects_structured_fact_refusal
    }
    recorded = {case.id for case in ALL_CASES if case.defect}
    assert observed_failures == recorded, (
        "The structured-fact gate changed. Newly failing: "
        f"{sorted(observed_failures - recorded)}; newly fixed: "
        f"{sorted(recorded - observed_failures)}. Update tests/rag_eval/eval_set.py "
        "and docs/rag-findings.md together."
    )


def test_the_measured_error_rates_are_reported() -> None:
    """Not a pass/fail on quality — a printed score, so a change is visible.

    The assertion is only that the eval set is big enough to mean something. The
    numbers themselves belong in `docs/rag-findings.md`.
    """
    under = [c for c in STRUCTURED_FACT_CASES if not is_structured_fact_question(c.question)]
    over = [
        c
        for c in POLICY_CASES + NO_SOURCE_CASES
        if is_structured_fact_question(c.question)
        and c.expected is not Expected.REFUSED_STRUCTURED_FACT
    ]
    print(
        f"\nstructured-fact gate: under-refusal {len(under)}/{len(STRUCTURED_FACT_CASES)}"
        f" -> {[c.id for c in under]}"
    )
    print(
        f"structured-fact gate: over-refusal  {len(over)}/{len(POLICY_CASES + NO_SOURCE_CASES)}"
        f" -> {[c.id for c in over]}"
    )
    assert len(ALL_CASES) >= 25
