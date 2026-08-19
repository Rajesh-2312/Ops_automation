"""Payout agent. CLAUDE.md §8: "Explain validation failures, draft variance
reasons, run summaries". Ceiling: Draft.

R2 IS THIS MODULE'S ENTIRE SPECIFICATION
========================================
    R2  "All monetary arithmetic lives in `services/remuneration/engine.py`, is
         pure Python, uses `Decimal`, and is unit-tested. An agent may explain a
         number. It may never produce one."

Every other specialist in §8 can treat R2 as a rule it happens not to break.
This is the one agent whose whole job is to talk about money, so here the rule
has to be structural or it is nothing. Four properties carry it, and each is
checked rather than asserted in prose:

1. **No engine, no validators, no invoice numbering.** Nothing under
   `app/agents/` imports `app.services.remuneration`, and
   `tests/unit/test_agents_payout.py` parses every file in the package to prove
   it. This module is typed against `app.domain.payout.PayoutResult` — the
   engine's *output* type, zero I/O, zero arithmetic — so it can hold a computed
   payout and has no way to obtain one. `compute_payout` is not importable from
   here in the sense that matters: it is not imported, and the test fails the
   build if it ever is.

2. **This module contains no arithmetic at all.** Not one `+`, `-`, `*`, `/`,
   `//`, `%` or `**`; no `sum()`, no `round()`, no `float`. A test walks this
   file's AST and fails on any arithmetic operator, so the property is
   maintained by CI rather than by memory. Counting failed gates is `len()` — a
   count of gates is not money — and every figure that reaches a prompt does so
   through `str()` on a `Decimal` the engine produced. Note what this rules out
   even in helpful-looking form: no "gross minus TDS" sanity line, no percentage
   of the trailing average, no run total.

3. **Its toolset cannot return an amount.** `PAYOUT_TOOLS` is `read_program`,
   `search_corpus`, `save_draft` — the catalogue notes the absence deliberately.
   No tool in it returns a rupee, so the only route by which a figure can reach
   this agent is `PayoutFacts` / `RunLine`, handed in by the caller that ran the
   engine.

4. **The prose is checked against exactly those figures.**
   `AgentRuntime.generate()` runs `assert_grounded` over the model's output
   against the structured input, and refuses the draft — no retry, no repair —
   if the text states a figure that did not arrive. "Approximately 15,500" where
   the engine computed 15,484 is the failure this exists to make impossible.

WHERE THE GUARANTEE STOPS, STATED PLAINLY
=========================================
This agent guarantees that it adds no figure of its own. It does **not**
guarantee that the caller ran the engine: a caller that constructs a
`PayoutResult` by hand can put any number in front of the model, and grounding
will pass it, because grounding's question is "did this arrive as structured
input?" and the answer is yes. That boundary is real and belongs to the caller —
`app/api/payouts.py` obtains results from `compute_payout` and reports from
`validate_payout`, and that is where it is enforced. Nothing an agent can do
would move that line, which is why the line is documented instead of blurred.

WHAT ARRIVES FROM RAG, AND WHAT IT MAY NOT GROUND
=================================================
§9: "Structured facts (dates, amounts, counts) are **never** retrieved from RAG.
Query the database. RAG supplies policy and context only."

So retrieved SOP text goes into the prompt as wording context but is deliberately
**not** part of `structured_input`. Only the citation labels — document title and
section — are, because those come from the corpus index and a citation the model
may not write is a citation nobody can check. A figure lifted out of a policy
passage is therefore ungrounded, and the draft is refused.

That is a stricter reading than `intake.py` takes, and the difference is
intentional: intake's source document *is* the system of record for the values
it extracts, whereas here the system of record is the engine. A TDS rate quoted
out of an SOP page is precisely the number that must come from the engine
instead, and an SOP that has drifted from `DEFAULT_TDS_RATE` would otherwise
produce a confident, cited, wrong sentence.

§5's ASYMMETRY IS DECIDED IN PYTHON, NEVER BY THE MODEL
=======================================================
CRT counts payable days UP from `P` marks; bCAP counts DOWN from the period
length. An unmarked day therefore silently **pays** a bCAP trainer and silently
**underpays** a CRT trainer, which is why attendance completeness is a hard block
for CRT and a warning for bCAP (§5, §7). Getting that backwards in an explanation
would teach a Manager the opposite of the rule the validators enforce.

It is not left to the model to remember. `ATTENDANCE_SEMANTICS` is a table keyed
by `ProgramType`, `attendance_semantics()` reads it, the result travels in the
structured input, and the prompt instructs the model to use it verbatim rather
than reason about it. A test asserts both rows and asserts they are not swapped.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

import structlog
from pydantic import JsonValue

from app.agents.ports import Draft, ProgramSnapshot, RetrievedPassage
from app.agents.runtime import AgentRuntime, DraftOutcome
from app.agents.tools.catalog import AgentName
from app.domain.enums import (
    ArtifactType,
    AutonomyLevel,
    Corpus,
    LLMTask,
    Persona,
    ProgramType,
    RateBasis,
    ValidationCode,
    ValidationSeverity,
)
from app.domain.money import format_indian, round_rupees
from app.domain.payout import PayoutResult

__all__ = [
    "ATTENDANCE_SEMANTICS",
    "AttendanceSemantics",
    "PayoutAgent",
    "PayoutFacts",
    "RunLine",
    "RunTally",
    "ValidationIssueLike",
    "attendance_semantics",
    "tally_run",
]

_log = structlog.get_logger(__name__)


# --- §5's asymmetry, as a table -----------------------------------------------


@dataclass(frozen=True, slots=True)
class AttendanceSemantics:
    """How one program type turns attendance into payable days. CLAUDE.md §5.

    Every field is a sentence the model is told to reuse rather than derive. The
    dangerous question — "does an unmarked day pay?" — has opposite answers for
    the two program types, and a model asked to reason it out will be right most
    of the time, which is the worst available failure rate for a rule that
    decides whether a trainer is underpaid without anybody noticing.
    """

    program_type: ProgramType
    rate_basis: RateBasis
    #: "counted UP from P marks" / "counted DOWN from the period length".
    payable_days_direction: str
    #: What an unmarked day does to the money, and to whose disadvantage.
    unmarked_day_effect: str
    #: §7's severity for the attendance-completeness gate on this program type.
    completeness_severity: ValidationSeverity

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "program_type": self.program_type.value,
            "rate_basis": self.rate_basis.value,
            "payable_days_direction": self.payable_days_direction,
            "unmarked_day_effect": self.unmarked_day_effect,
            "attendance_completeness_gate": self.completeness_severity.value,
        }


#: CLAUDE.md §5, transcribed. Two rows, and the second is not the first with a
#: word changed — read them together before editing either.
ATTENDANCE_SEMANTICS: Final[Mapping[ProgramType, AttendanceSemantics]] = MappingProxyType(
    {
        ProgramType.CRT: AttendanceSemantics(
            program_type=ProgramType.CRT,
            rate_basis=RateBasis.PER_DAY,
            payable_days_direction="counted UP from P marks",
            unmarked_day_effect=(
                "an unmarked day is NOT payable, so every unmarked day silently UNDERPAYS "
                "the trainer"
            ),
            completeness_severity=ValidationSeverity.BLOCKING,
        ),
        ProgramType.BCAP: AttendanceSemantics(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            payable_days_direction="counted DOWN from the period length",
            unmarked_day_effect=(
                "an unmarked day IS payable, so every unmarked day silently PAYS the "
                "trainer regardless of whether they attended"
            ),
            completeness_severity=ValidationSeverity.WARNING,
        ),
    }
)


#: The money fields of a `PayoutResult`, for the whole-rupee display form (§11:
#: "Decimal, two-place storage, whole-rupee display"). Named explicitly rather
#: than derived from `PayoutResult.NUMERIC_FIELDS`, which also carries
#: `payable_days` and `tds_rate` — rounding either of those to rupees would be
#: nonsense. `net_unrounded` is absent because its display form is `net`, and two
#: keys rendering the same figure invite a reader to think they differ.
_DISPLAY_MONEY_FIELDS: Final[tuple[str, ...]] = (
    "rate_per_day",
    "earned",
    "reimbursements",
    "gross",
    "tds",
    "deductions",
    "net",
)


def attendance_semantics(program_type: ProgramType) -> AttendanceSemantics:
    """§5's row for one program type. Raises `KeyError` on an unmapped type.

    No default. A third program type arriving without an explicit row must fail
    loudly here rather than inherit whichever set of semantics happens to be
    first in the table — that inheritance is exactly how a per-day engagement
    would start being explained as though unmarked days were paid.
    """
    return ATTENDANCE_SEMANTICS[program_type]


# --- what the caller hands in -------------------------------------------------


class ValidationIssueLike(Protocol):
    """One §7 gate outcome, structurally.

    A `Protocol` and not an import of
    `app.services.remuneration.validators.ValidationIssue`, for two reasons that
    point the same way. Layering: nothing under `app/agents/` imports
    `app/services/` — `app.services.reporting.narration` says the same thing from
    the other side, redeclaring its `Completer` "to keep `app/services/` from
    depending on `app/agents/`". And R2: importing the validators module would
    put `validate_payout` in this agent's namespace, and an agent that can *run*
    the gates is one edit away from an agent that decides their outcome instead
    of explaining it.

    Members are read-only properties rather than attributes so a frozen
    dataclass satisfies the protocol. `ValidationIssue` does, exactly as written.
    """

    @property
    def code(self) -> ValidationCode: ...

    @property
    def severity(self) -> ValidationSeverity: ...

    @property
    def message(self) -> str: ...

    @property
    def detail(self) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class PayoutFacts:
    """One trainer-month, exactly as the engine and the §7 gates left it.

    Assembled by the caller, never by this module. `result` is what
    `compute_payout()` returned and `issues` is what `validate_payout()` reported;
    both travel whole rather than as a handful of extracted fields, so a dispute
    can be walked line by line against the same object the sheet was printed from.

    Identity is PAN (§6: "Trainer identity is PAN. Never match trainers by name
    string"). `trainer_name` is carried for the prose and for nothing else.
    """

    trainer_name: str
    trainer_pan: str
    program_type: ProgramType
    period_start: dt.date
    period_end: dt.date
    result: PayoutResult
    issues: tuple[ValidationIssueLike, ...] = ()
    invoice_number: str | None = None

    @property
    def semantics(self) -> AttendanceSemantics:
        return attendance_semantics(self.program_type)

    @property
    def blocking(self) -> tuple[ValidationIssueLike, ...]:
        return tuple(i for i in self.issues if i.severity is ValidationSeverity.BLOCKING)

    @property
    def warnings(self) -> tuple[ValidationIssueLike, ...]:
        return tuple(i for i in self.issues if i.severity is ValidationSeverity.WARNING)

    @property
    def is_blocked(self) -> bool:
        """True when §7 forbids this cycle reaching PENDING_APPROVAL."""
        return bool(self.blocking)

    def as_payload(self) -> dict[str, JsonValue]:
        """The facts as structured input. Every amount a string (R7).

        Stringified rather than passed as a number because `json.dumps` renders a
        `float` and a `Decimal` differently and only one of them is the figure the
        engine computed. `str(Decimal("15484.00"))` is `'15484.00'`, which the
        grounding check normalises back to the same value the model may quote in
        whatever format it likes.
        """
        return {
            "trainer": {"name": self.trainer_name, "pan": self.trainer_pan},
            "period": {
                "start": self.period_start.isoformat(),
                "end": self.period_end.isoformat(),
            },
            "invoice_number": self.invoice_number,
            "attendance_semantics": self.semantics.as_payload(),
            "payout": _result_payload(self.result),
            "blocking": [_issue_payload(i) for i in self.blocking],
            "warnings": [_issue_payload(i) for i in self.warnings],
            "blocking_count": len(self.blocking),
            "warning_count": len(self.warnings),
            "is_blocked": self.is_blocked,
        }


@dataclass(frozen=True, slots=True)
class RunLine:
    """One trainer-month in a payout run, as a run summary sees it.

    `net` is `None` when no payout has been computed for that deployment yet —
    which is a real state and a useful finding, and is reported as an absence
    rather than as a zero. A zero would be a figure, and a figure this agent did
    not receive is a figure it must not state.
    """

    trainer_name: str
    trainer_pan: str
    program_type: ProgramType
    net: Decimal | None = None
    blocking_codes: tuple[ValidationCode, ...] = ()
    warning_codes: tuple[ValidationCode, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking_codes)

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "trainer": {"name": self.trainer_name, "pan": self.trainer_pan},
            "program_type": self.program_type.value,
            "net": str(self.net) if self.net is not None else None,
            "blocking": [code.value for code in self.blocking_codes],
            "warnings": [code.value for code in self.warning_codes],
        }


@dataclass(frozen=True, slots=True)
class RunTally:
    """Counts across a run. Counts, and deliberately nothing else.

    THERE IS NO TOTAL, AND THAT IS THE POINT
    ----------------------------------------
    A run summary is where "what did this month cost?" gets asked, and a sum of
    net pays written here would be a second implementation of money living in an
    agent — untested, unfixtured, and the first thing to disagree with Finance.
    `app.services.reporting.assembly.TrainerCostSection` refuses the same total
    for the same reason, and `app/api/payouts.py` says it too: "a sum computed in
    a router is a second, untested implementation of money". If a run total is
    wanted it belongs in the engine with a fixture behind it.

    `as_payload()` states the absence explicitly rather than omitting the key, so
    the model is told there is no total instead of being left to notice.
    """

    trainer_count: int
    blocked_count: int
    warning_count: int
    clear_count: int
    uncomputed_count: int
    blocking_code_counts: Mapping[str, int]
    warning_code_counts: Mapping[str, int]

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "trainer_count": self.trainer_count,
            "blocked_count": self.blocked_count,
            "warning_count": self.warning_count,
            "clear_count": self.clear_count,
            "uncomputed_count": self.uncomputed_count,
            "blocking_code_counts": dict(self.blocking_code_counts),
            "warning_code_counts": dict(self.warning_code_counts),
            "total_net": None,
            "total_note": (
                "No total is computed. CLAUDE.md R2 puts all monetary arithmetic in "
                "services/remuneration/engine.py, unit-tested; a total added here would "
                "be a second implementation of money."
            ),
        }


def tally_run(lines: Sequence[RunLine]) -> RunTally:
    """Count a run's outcomes. Pure, deterministic, and counting only.

    `Counter` over gate codes rather than a model reading a list and reporting
    "mostly missing work orders": a count is a fact, and the one number in a run
    summary a Manager acts on first is how many cycles are blocked and by what.

    Every figure produced here is a count of records. No amount is read, added or
    compared — `net` is not even inspected except for its presence.
    """
    blocking: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    for line in lines:
        blocking.update(code.value for code in line.blocking_codes)
        warnings.update(code.value for code in line.warning_codes)
    return RunTally(
        trainer_count=len(lines),
        blocked_count=len([line for line in lines if line.is_blocked]),
        warning_count=len([line for line in lines if line.warning_codes]),
        clear_count=len(
            [line for line in lines if not line.blocking_codes and not line.warning_codes]
        ),
        uncomputed_count=len([line for line in lines if line.net is None]),
        blocking_code_counts=MappingProxyType(dict(blocking)),
        warning_code_counts=MappingProxyType(dict(warnings)),
    )


# --- prompts ------------------------------------------------------------------
#
# Each states R2 to the model in its own words. The prompt is courtesy; the
# grounding check is the enforcement. Rule 1 of each is deliberately the same
# sentence, because it is the one that matters.

_ARITHMETIC_RULE: Final[str] = (
    "1. Every rupee figure, day count, rate, percentage and date has already been "
    "computed and is given to you. Quote them exactly as given. Do not add, subtract, "
    "multiply, divide, total, average, prorate, estimate or round ANYTHING.\n"
    "2. State no number that is not in the structured data. If a number would help and "
    "is not there, say it is not recorded rather than supplying one.\n"
    "3. Amounts appear twice: `display` is the whole-rupee form to write in prose, and "
    "`exact` is the engine's full precision, for a dispute. Quote one of the two exactly "
    "as written. Never round `exact` yourself.\n"
)

_FAILURE_SYSTEM: Final[str] = (
    "You explain to a byteXL operations manager why one trainer's payout cycle did not "
    "pass validation, and what a human has to do before it can move to approval.\n"
    "\n"
    f"Rules you must follow exactly:\n{_ARITHMETIC_RULE}"
    "4. Work through the blocking failures first, then the warnings, in the order given. "
    "For each, say what the gate checks, what the data shows, and who must fix what.\n"
    "5. The attendance semantics for this programme type are given to you in "
    "`attendance_semantics`. Use them exactly as written. Do not reason about which "
    "programme type pays for an unmarked day — the answer is in the data and it is "
    "opposite for the two types.\n"
    "6. Policy passages are supplied for wording only. Never take a figure from one.\n"
    "7. Do not describe anything as approved, submitted, released or paid. A human "
    "reads, edits and acts on this."
)

_VARIANCE_SYSTEM: Final[str] = (
    "You draft wording for a variance reason that a byteXL operations manager will "
    "review, edit, and then state as their own against a payout warning.\n"
    "\n"
    f"Rules you must follow exactly:\n{_ARITHMETIC_RULE}"
    "4. Say what the warning is, what the given figures show, and what would have to be "
    "true for the payout to be correct anyway.\n"
    "5. Never assert a cause you were not given. Where the data does not say why, write "
    "that the reason must be supplied by whoever knows it.\n"
    "6. This is not a stated reason until a human states it. Do not write as though it "
    "has been accepted, agreed or recorded.\n"
    "7. Four sentences at most."
)

_RUN_SUMMARY_SYSTEM: Final[str] = (
    "You summarise one byteXL payout run for an operations manager who is about to work "
    "through it.\n"
    "\n"
    f"Rules you must follow exactly:\n{_ARITHMETIC_RULE}"
    "4. Do NOT total, sum or average the net pay figures. No total is given to you "
    "because none has been computed; if a total is wanted, say it has to come from the "
    "payout engine.\n"
    "5. Lead with how many cycles are blocked and by which gate.\n"
    "6. Name the trainers whose cycles are blocked. Do not speculate about causes.\n"
    "7. Six sentences at most."
)

_POLICY_HEADER: Final[str] = (
    "POLICY CONTEXT (wording only — CLAUDE.md §9: policy and context, never figures. "
    "Do not quote any number that appears below):"
)


# --- the agent ----------------------------------------------------------------


@dataclass
class PayoutAgent:
    """Explains payouts. Never computes one. Level 2, Draft (§8).

    Holds a runtime and nothing else. There is no engine handle, no session, no
    rate table and no invoice sequence — the things that would let this class
    produce a figure are absent rather than guarded.
    """

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.PAYOUT:
            raise ValueError(
                f"PayoutAgent needs the payout runtime, got '{self.runtime.agent.value}'"
            )
        if self.runtime.autonomy > AutonomyLevel.DRAFT:
            raise ValueError(
                "Payout's ceiling is Draft (CLAUDE.md §8), and §8 is unconditional about "
                "it: 'Nothing touching money, contracts, or a college contact goes past "
                "level 3.' This agent explains a payout; a human approves and releases it."
            )

    async def explain_validation_failures(
        self,
        program_id: UUID,
        facts: PayoutFacts,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Explain the §7 gate outcomes for one trainer-month. Volume tier (§2).

        The gates have already run. This method reads the programme for context,
        pulls SOP wording for the codes that failed, and asks the model to turn
        `ValidationIssue.message` and `ValidationIssue.detail` — both produced by
        `validators.py` from structured data — into something a Manager can act
        on.

        Raises `UngroundedFigureError` if the explanation states a figure the
        engine and the gates did not produce. Not retried: see
        `app.agents.grounding` on why a retry hides a systematic problem behind
        an occasional extra call.
        """
        program = await self.runtime.dispatcher.read_program(program_id)
        passages = await self.runtime.dispatcher.search_corpus(
            Corpus.SOP.value, _policy_query(facts)
        )

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            **facts.as_payload(),
            # Labels only. The passage TEXT is in the prompt and NOT here, so a
            # figure taken out of an SOP page is ungrounded — see the module
            # docstring, and §9.
            "sources": _source_labels(passages),
        }
        body, invocation = await self.runtime.generate(
            LLMTask.DRAFTING,
            system=_FAILURE_SYSTEM,
            user=_prompt(
                "PAYOUT VALIDATION OUTCOME (the only figures you may state)",
                grounded_in,
                passages,
            ),
            structured_input=grounded_in,
            context="payout.explain_validation_failures",
        )

        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Payout validation — {facts.trainer_name}, {facts.period_start.isoformat()}",
            body=body,
            payload={
                "trainer_pan": facts.trainer_pan,
                "blocking": [_issue_payload(i) for i in facts.blocking],
                "warnings": [_issue_payload(i) for i in facts.warnings],
                "is_blocked": facts.is_blocked,
            },
            program_id=program_id,
            flags=_failure_flags(facts, program),
            grounded_in=grounded_in,
        )
        _log.info(
            "payout.validation_explained",
            program_id=str(program_id),
            program_type=facts.program_type.value,
            blocking=len(facts.blocking),
            warnings=len(facts.warnings),
            blocked=facts.is_blocked,
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_variance_reason(
        self,
        program_id: UUID,
        facts: PayoutFacts,
        code: ValidationCode,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Propose wording for the stated reason a §7 warning requires.

        §7's warnings are "permitted, but requires a stated reason", and
        `ValidationReport.can_submit()` will not let a cycle move to
        PENDING_APPROVAL until every warning has one. That makes the stated reason
        a gate, and a gate an agent could satisfy on its own would be an agent
        that unblocks a payout — level 3 behaviour on money, which §8 forbids.

        So this returns wording, flagged as unstated, in DRAFT. Nothing here can
        attach it to a run as the reason: the payload carries `"stated": false`,
        this agent holds no tool that could write a run, and R4 keeps the artifact
        in DRAFT until a human moves it.

        `code` must be one of the warnings actually raised. Drafting a reason for
        a gate that did not fire would be explaining a fact that is not in the
        record, which is R1 in its plainest form.
        """
        warning = next((issue for issue in facts.warnings if issue.code is code), None)
        if warning is None:
            raised = ", ".join(issue.code.value for issue in facts.warnings) or "none"
            raise ValueError(
                f"No warning {code.value!r} was raised for this payout (warnings raised: "
                f"{raised}). CLAUDE.md R1 — an agent may not explain a gate outcome that "
                "is not in the record."
            )

        grounded_in: dict[str, JsonValue] = {
            "warning": _issue_payload(warning),
            **facts.as_payload(),
        }
        body, invocation = await self.runtime.generate(
            LLMTask.DRAFTING,
            system=_VARIANCE_SYSTEM,
            user=_prompt(
                "PAYOUT WARNING NEEDING A STATED REASON (the only figures you may state)",
                grounded_in,
                (),
            ),
            structured_input=grounded_in,
            context="payout.draft_variance_reason",
        )

        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Variance reason (proposed) — {code.value} — {facts.trainer_name}",
            body=body,
            payload={
                "trainer_pan": facts.trainer_pan,
                "code": code.value,
                "proposed_reason": body,
                # Never true from this path, and there is no code here that could
                # set it. §7's reason is stated by a human or it is not stated.
                "stated": False,
            },
            program_id=program_id,
            flags=(
                "PROPOSED WORDING ONLY — CLAUDE.md §7 requires a stated reason; a reason "
                "an agent wrote is not a reason a human stated",
                f"warning {code.value} still blocks submission until a human states a reason",
            ),
            grounded_in=grounded_in,
        )
        _log.info(
            "payout.variance_reason_drafted",
            program_id=str(program_id),
            code=code.value,
            program_type=facts.program_type.value,
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_run_summary(
        self,
        program_id: UUID,
        lines: Sequence[RunLine],
        *,
        period_label: str,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Summarise a payout run. Volume tier — §2 routes summaries there.

        The tally is computed by `tally_run()` before the model is called, so the
        prose describes counts that were counted rather than counts that were
        noticed. No total is computed, and `RunTally.as_payload()` tells the model
        so in as many words.

        `period_label` is the caller's ("July 2026", "2026-07"); it travels in the
        structured input so its digits are grounded and the model may write them.
        """
        program = await self.runtime.dispatcher.read_program(program_id)
        tally = tally_run(lines)

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            "period_label": period_label,
            "tally": tally.as_payload(),
            "lines": [line.as_payload() for line in lines],
        }
        body, invocation = await self.runtime.generate(
            LLMTask.SUMMARY,
            system=_RUN_SUMMARY_SYSTEM,
            user=_prompt(
                "PAYOUT RUN (already counted — the only figures you may state)",
                grounded_in,
                (),
            ),
            structured_input=grounded_in,
            context="payout.draft_run_summary",
        )

        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Payout run summary — {period_label}",
            body=body,
            payload={"period_label": period_label, "tally": tally.as_payload()},
            program_id=program_id,
            flags=_run_flags(tally),
            grounded_in=grounded_in,
        )
        _log.info(
            "payout.run_summarised",
            program_id=str(program_id),
            period=period_label,
            trainers=tally.trainer_count,
            blocked=tally.blocked_count,
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )


# --- flags: what the reviewer is being asked to look at -----------------------


def _failure_flags(facts: PayoutFacts, program: ProgramSnapshot | None) -> tuple[str, ...]:
    """Non-blocking observations for the human holding the draft.

    The programme-type mismatch check is the one worth reading twice. §5 makes
    attendance semantics a function of programme type, so a payout computed as
    bCAP against a programme the tracker calls CRT has been prorated when it
    should have been counted up — the figures are internally consistent and
    wrong, which is the hardest kind of wrong to spot in a sheet.
    """
    flags: list[str] = []
    if facts.is_blocked:
        flags.append(
            f"BLOCKED — {len(facts.blocking)} blocking gate(s); the cycle cannot reach "
            "PENDING_APPROVAL until every one is cleared (CLAUDE.md §7)"
        )
    if facts.warnings:
        flags.append(
            f"{len(facts.warnings)} warning(s), each needing a stated reason from a human "
            "before submission (CLAUDE.md §7)"
        )
    if not facts.issues:
        flags.append("no §7 gate failed — this explanation covers a clean cycle")
    if program is None:
        flags.append(
            "programme not readable in this session's scope (CLAUDE.md §4) — explained "
            "without programme context"
        )
    elif program.program_type != facts.program_type.value:
        flags.append(
            f"programme type mismatch: the tracker says '{program.program_type}', the payout "
            f"was computed as '{facts.program_type.value}'. CLAUDE.md §5 makes payable-day "
            "counting depend on this — check before approving"
        )
    return tuple(flags)


def _run_flags(tally: RunTally) -> tuple[str, ...]:
    flags: list[str] = []
    if tally.blocked_count:
        flags.append(
            f"{tally.blocked_count} of {tally.trainer_count} cycle(s) blocked by a §7 gate"
        )
    if tally.uncomputed_count:
        flags.append(f"{tally.uncomputed_count} trainer-month(s) have no computed payout on file")
    if not tally.trainer_count:
        flags.append("the run is empty — no trainer-month was included")
    flags.append("no run total is stated: CLAUDE.md R2 keeps monetary arithmetic in the engine")
    return tuple(flags)


# --- payload helpers ----------------------------------------------------------


def _result_payload(result: PayoutResult) -> dict[str, JsonValue]:
    """The engine's result as JSON, every amount a string (R7).

    TWO FORMS OF THE SAME FIGURE, AND WHY BOTH ARE NEEDED
    -----------------------------------------------------
    `exact` is what the engine computed, to full precision:
    `earned` on the VEMA fixture is `15483.87096774193548387096774`, because R6
    carries full precision through every intermediate and rounds once, at net.
    That string is the truth and it is unreadable.

    `display` is the same figures at §11's stated presentation precision —
    "Decimal, two-place storage, whole-rupee display" — rendered by
    `app.domain.money.round_rupees` and `format_indian`, the codebase's own
    helpers, unit-tested in `tests/unit/test_money.py`. On the VEMA fixture it
    reproduces CLAUDE.md §6's table exactly: earned 15,484 · gross 15,584 ·
    TDS 1,548 · net 14,035.

    Without `display` the agent could not write a readable sentence about money:
    the grounding check compares by numeric value, so a model writing the natural
    "₹15,484" against an exact value of `15483.870967…` would have its draft
    refused, every time, for being right in the way a human would be.

    This does not weaken R2, and the distinction is the whole point of the rule.
    R2 forbids an LLM *computing* money; both forms here are produced by tested
    Python from a figure the engine returned. R6 permits rounding "at final
    display", which is exactly what this is, and forbids a rounded value
    re-entering a calculation — impossible here, because this module contains no
    arithmetic at all and a test proves it.

    `exact` is driven off `PayoutResult.NUMERIC_FIELDS`, so a field added to the
    engine's result appears automatically rather than being silently dropped from
    every explanation. `display` names the money fields explicitly, because
    whole-rupee rounding is meaningless for `payable_days` and `tds_rate`.
    """
    exact: dict[str, JsonValue] = {
        name: str(getattr(result, name)) for name in PayoutResult.NUMERIC_FIELDS
    }
    display: dict[str, JsonValue] = {
        name: format_indian(round_rupees(getattr(result, name))) for name in _DISPLAY_MONEY_FIELDS
    }
    return {
        "rate_basis": result.rate_basis.value,
        "days_in_month": result.days_in_month,
        "payable_days": str(result.payable_days),
        "exact": exact,
        "display": display,
    }


def _issue_payload(issue: ValidationIssueLike) -> dict[str, JsonValue]:
    """One gate outcome as JSON.

    `detail` is already `Mapping[str, str]` on the validators' side — the figures
    in it were stringified by `_issue()` there — so nothing is converted here.
    `message` carries figures too, and it is the validators' sentence, not a
    model's.
    """
    return {
        "code": issue.code.value,
        "severity": issue.severity.value,
        "message": issue.message,
        "detail": dict(issue.detail),
    }


def _program_payload(program: ProgramSnapshot | None) -> JsonValue:
    """The programme snapshot as JSON, or `None` when out of reach (§4 RLS).

    No commercials on `ProgramSnapshot` by design — the type's docstring explains
    why — so this cannot widen what an LDE Executive's session can see.
    """
    if program is None:
        return None
    return {
        "college_name": program.college_name,
        "program_type": program.program_type,
        "stage": program.stage.value,
    }


def _source_labels(passages: Sequence[RetrievedPassage]) -> list[JsonValue]:
    """Citation labels for retrieved passages. §9: no citation, no answer.

    Labels, not text. A passage cannot be constructed without a document title
    and a section, so every source here is citable; the body stays out of the
    grounded set so no figure can be lifted from it.
    """
    return [
        {"document": passage.document_title, "section": passage.section} for passage in passages
    ]


def _policy_query(facts: PayoutFacts) -> str:
    """A deterministic corpus query from the gates that actually failed.

    Built in Python from enum values rather than asked of the model: a retrieval
    query the model composes is a query nobody can reproduce when the answer
    turns out to be wrong.
    """
    codes = ", ".join(sorted({issue.code.value for issue in facts.issues}))
    if not codes:
        return "payout validation gates and approval prerequisites"
    return f"payout validation gate policy: {codes}"


def _prompt(
    header: str, payload: Mapping[str, JsonValue], passages: Sequence[RetrievedPassage]
) -> str:
    """Structured facts as JSON, then policy passages as words.

    Two clearly separated blocks — §9 requires hybrid answers to "visibly
    separate the two", and the separation is what lets the prompt say "figures
    from the first block only" and mean something.
    """
    blocks = [header, json.dumps(payload, indent=2, sort_keys=True, default=str)]
    if passages:
        blocks.append(_POLICY_HEADER)
        blocks.extend(
            f"{passage.document_title} — {passage.section}\n{passage.text}" for passage in passages
        )
    return "\n\n".join(blocks)
