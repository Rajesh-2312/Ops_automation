"""Delivery Monitor. CLAUDE.md §8: "Attendance, usage, syllabus anomalies, risk
scoring". Ceiling: **Alert (internal only)** — autonomy level 1, Observe.

THIS AGENT IS A LANGUAGE LAYER, NOT A SECOND OPINION
====================================================
`app/services/monitoring/` and `app/services/escalation/` already do the work
this agent is named after, and they do it in pure, deterministic Python:
`detect_anomalies()` names what is wrong, `score_anomalies()` and
`band_for_score()` say how bad it is, `build_alert()` renders it for an internal
audience, and `evaluate_sla()` decides whether it climbs the §4 ladder. §8 is
explicit that the Escalation Engine is "deterministic SLA rules. Not LLM
judgement," and the same reasoning governs scoring: a risk number that moves
between runs is a risk number people stop believing.

So this module contains **no detector, no threshold, no weight, no comparison and
no score**. Grep it for a numeric literal and you will find none. What it adds is
the one thing the deterministic layer cannot produce: a paragraph a Manager can
read on a Monday morning. R1 states the division exactly — the database owns
truth, the LLM owns language — and here the "database" is a `RiskAssessment` the
caller computed before this agent was called.

If you find yourself computing a risk number in here, that is a defect, not an
enhancement. Add the rule to `app/services/monitoring/detectors.py`, where it is
testable, stable and explainable line by line.

NOT MEASURED IS NOT ZERO
========================
The engine's own rule (`app/services/escalation/engine.py`) is that an absent
metric means "not measured", never "zero" — otherwise every `LT` rule fires on
every subject nobody collected data for. Prose has the same failure mode and it
is worse, because a paragraph that omits usage reads as a paragraph that checked
usage and found nothing wrong.

Today five of the nine detectors cannot fire at all — the two usage ones and the
three syllabus ones. There is no platform-usage table (§14 Q6 — "EdTech platform
access: direct DB, API, or neither?" — is open) and no column stores a
syllabus-completion reading. (`app/api/monitoring.py` says "four" in its
docstring and then lists five function names; the count here is the tuple in
`app/services/monitoring/detectors.py`, and `SIGNAL_FAMILY_CODES` below is
asserted total over `AnomalyCode` so it cannot drift.) `MonitorContext.usage` and
`.syllabus` therefore arrive as `None`, the detectors stay silent, and this
module reports that silence as **unobserved** rather than as health.
`signal_coverage()` computes that from the context — which signal families were
supplied, not what they said — and the briefing prompt forbids describing an
unobserved family as fine, clean or zero. Mapping `attendance_records` (student
class attendance) onto a `UsageSignal` would make the agent state a delivery fact
the platform never reported, which is the R1 breach that matters most here.

TWO GUARDS, BECAUSE GROUNDING ONLY CATCHES NUMBERS
==================================================
`AgentRuntime.generate()` runs `assert_grounded`, so a figure the model invented
raises. That leaves one gap: an anomaly is a *name*, not a number, and "no
learner activity on the platform" contains no digits. `assert_only_detected_
anomalies()` closes it — every `AnomalyCode` is a distinctive snake_case
identifier, so a briefing that names one which did not fire is refused. Between
the two, the agent can neither invent a figure nor invent a finding.

NOBODY IS CONTACTED, AND NOBODY CAN BE
======================================
`MONITOR_TOOLS` is three read tools and no `save_draft` (R3), so this agent has
no write capability of any kind — not even the draft that Intake and Sourcing
hold. Its output is a value returned to the caller for an internal dashboard,
exactly as the supervisor's is. `build_alert()` refuses an external audience, so
a briefing addressed at a college or a trainer cannot be constructed, and the
agent additionally filters the program's contacts to `INTERNAL_PERSONAS` before
the model sees a single name.

`MonitorContext.labels` — which may carry a trainer PAN (§6) — is deliberately
**not** put in the prompt. It travels on the briefing for the dashboard. The
narrative is about a program, not a person, and the gateway (§2) does not need
the identifier to write it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import structlog
from pydantic import JsonValue

from app.agents.ports import ContactSnapshot, ProgramSnapshot, TaskSnapshot
from app.agents.runtime import AgentInvocation, AgentRuntime
from app.agents.tools.catalog import AgentName
from app.domain.enums import (
    INTERNAL_PERSONAS,
    AutonomyLevel,
    LLMTask,
    Persona,
    ProgramStage,
    TaskStatus,
)
from app.domain.risk import AnomalyCode
from app.services.escalation.engine import EscalationDecision
from app.services.monitoring.alerts import InternalAlert, build_alert
from app.services.monitoring.scoring import RiskAssessment
from app.services.monitoring.signals import MonitorContext

__all__ = [
    "SIGNAL_FAMILY_CODES",
    "FabricatedAnomalyError",
    "MonitorAgent",
    "MonitorBriefing",
    "SignalCoverage",
    "SignalFamily",
    "SignalState",
    "assert_only_detected_anomalies",
    "briefing_payload",
    "signal_coverage",
]

_log = structlog.get_logger(__name__)


_BRIEFING_SYSTEM: Final[str] = (
    "You write a short internal briefing for byteXL operations staff about one college "
    "program's delivery risk. Every anomaly, the risk score and the band were computed "
    "deterministically before you were called.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Explain what was observed. Do not re-score, re-rank, re-classify or second-guess "
    "anything you are given, and do not compute anything.\n"
    "2. Use ONLY the figures, dates, counts and percentages in the structured data. State "
    "no number that is not there, and quote every number exactly as given.\n"
    "3. Name only anomalies listed under `anomalies`. A signal family listed as "
    "`not_measured` was NOT checked: never call it fine, healthy, clean, on track, stable "
    "or zero. Say it was not observed in this run.\n"
    "4. Report; do not instruct. No recommendations, no action items, no deadlines and no "
    "judgement about who is at fault.\n"
    "5. This is read on an internal byteXL dashboard. No greeting, no sign-off, nothing "
    "addressed to any person, and nothing addressed to a college or a trainer.\n"
    "6. Six sentences at most. Do not cite rule, section or clause numbers."
)


# --- signal coverage: what was looked at, which is not what was found --------


class SignalFamily(StrEnum):
    """The three input families §8 gives the Delivery Monitor.

    A vocabulary owned by one module rather than a status stored in a column, so
    it lives here and not in `app/domain/enums.py` — the precedent
    `app.agents.tools.catalog.ToolEffect` and `app.core.audit.AuditAction` set,
    and for the same reason: keeping it beside the mapping it closes is what lets
    a test assert the mapping is total.
    """

    ATTENDANCE = "attendance"
    USAGE = "usage"
    SYLLABUS = "syllabus"


class SignalState(StrEnum):
    """Whether a family was supplied to this run at all.

    Two members, and there is deliberately no `CLEAN`. "Observed and nothing
    fired" is read off the anomaly list; conflating it with "not supplied" is the
    exact mistake `evaluate_sla()` refuses to make when it skips an absent
    metric instead of defaulting it to zero.
    """

    OBSERVED = "observed"
    NOT_MEASURED = "not_measured"


#: Which anomaly codes each family's detectors can raise. Total over
#: `AnomalyCode` — `tests/unit/test_agents_monitor.py` asserts every member
#: appears exactly once, so a new code cannot be added upstream without this
#: mapping being updated in the same breath.
#:
#: This is a grouping for reporting what could not be evaluated. It is not a
#: detector and it decides nothing; the codes themselves are raised by
#: `app/services/monitoring/detectors.py` and nowhere else.
SIGNAL_FAMILY_CODES: Final[Mapping[SignalFamily, tuple[AnomalyCode, ...]]] = MappingProxyType(
    {
        SignalFamily.ATTENDANCE: (
            AnomalyCode.ATTENDANCE_UNMARKED_DAYS,
            AnomalyCode.ATTENDANCE_MARKING_STALE,
            AnomalyCode.ATTENDANCE_ABSENCE_STREAK,
            AnomalyCode.ATTENDANCE_ABSENCE_RATE,
        ),
        SignalFamily.USAGE: (
            AnomalyCode.USAGE_LOW_ACTIVE_SHARE,
            AnomalyCode.USAGE_NO_ACTIVITY,
        ),
        SignalFamily.SYLLABUS: (
            AnomalyCode.SYLLABUS_BEHIND_SCHEDULE,
            AnomalyCode.SYLLABUS_STALLED,
            AnomalyCode.SYLLABUS_IMPLAUSIBLE,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class SignalCoverage:
    """One family, and whether this run had anything to look at.

    `codes` is what stayed silent when `coverage` is `NOT_MEASURED`. It is
    carried for the dashboard and for tests and is deliberately **not** put in
    the prompt: naming `usage_no_activity` as unevaluable would put the string in
    front of the model, and `assert_only_detected_anomalies()` could then no
    longer tell an echo from an invention.
    """

    family: SignalFamily
    coverage: SignalState
    codes: tuple[AnomalyCode, ...]

    @property
    def is_measured(self) -> bool:
        return self.coverage is SignalState.OBSERVED


def signal_coverage(ctx: MonitorContext) -> tuple[SignalCoverage, ...]:
    """Which signal families were supplied. Pure, and it inspects nothing else.

    Presence, not content: this asks whether the caller had a reading, never
    whether the reading was good. Family order is `SignalFamily`'s declaration
    order, which is §8's own ordering, so a briefing reads the same way every
    week.
    """
    present: Mapping[SignalFamily, bool] = {
        SignalFamily.ATTENDANCE: ctx.attendance is not None,
        SignalFamily.USAGE: ctx.usage is not None,
        SignalFamily.SYLLABUS: ctx.syllabus is not None,
    }
    return tuple(
        SignalCoverage(
            family=family,
            coverage=SignalState.OBSERVED if present[family] else SignalState.NOT_MEASURED,
            codes=SIGNAL_FAMILY_CODES[family],
        )
        for family in SignalFamily
    )


# --- the second guard: a finding is a name, not a number --------------------


class FabricatedAnomalyError(RuntimeError):
    """The briefing named an anomaly that no detector raised.

    A `RuntimeError` rather than a `ValueError`, for the reason stated across
    this codebase: a broad `except ValueError` around parsing must not be able to
    swallow an R1 breach.

    This is the non-numeric half of grounding. `assert_grounded` catches an
    invented figure; nothing in it catches "no learner activity on the platform"
    written about a program whose usage was never measured. Refused, not
    corrected — a briefing with the offending sentence removed still reads as if
    somebody checked.
    """

    def __init__(self, codes: Sequence[AnomalyCode], context: str) -> None:
        listed = ", ".join(code.value for code in codes)
        super().__init__(
            f"{context}: the briefing names {len(codes)} anomaly code(s) that did not fire "
            f"in this run — {listed}. CLAUDE.md R1: an agent may not assert a fact it did "
            "not read from a system of record, and anomalies are detected in pure Python by "
            "app/services/monitoring/detectors.py. The Delivery Monitor explains findings; "
            "it does not produce them."
        )
        self.codes = tuple(codes)
        self.context = context


def assert_only_detected_anomalies(text: str, fired: Sequence[AnomalyCode], context: str) -> None:
    """Raise unless every anomaly code named in `text` actually fired.

    Matching is substring and case-insensitive over the code *values*, which are
    distinctive underscore identifiers (`usage_no_activity`) that do not occur in
    ordinary English. The check is therefore precise in the direction that
    matters — it will not fire on prose — while catching the one failure this
    agent must never have: a finding invented for a family nobody measured.

    Returns `None` on success. Never a boolean: a falsy refusal is one forgotten
    `if` away from shipping an invented finding onto an operations dashboard.
    """
    allowed = {code.value for code in fired}
    lowered = text.lower()
    invented = tuple(
        code for code in AnomalyCode if code.value not in allowed and code.value in lowered
    )
    if invented:
        raise FabricatedAnomalyError(invented, context)


# --- the structured input, built without a model ----------------------------


def _internal_only(contacts: Sequence[ContactSnapshot]) -> tuple[ContactSnapshot, ...]:
    """Drop external parties. §4's internal personas; §8's ceiling on this agent.

    A trainer and a college contact are external in §4 terms, and the Delivery
    Monitor's ceiling is "Alert (**internal only**)". Filtering here means an
    external name is not in the prompt at all, which is a stronger property than
    instructing the model not to address one.
    """
    return tuple(contact for contact in contacts if contact.persona in INTERNAL_PERSONAS)


def _open_monitoring_tasks(tasks: Sequence[TaskSnapshot]) -> tuple[TaskSnapshot, ...]:
    """Open tasks in the monitoring stage. Context for why a signal looks the way
    it does — an unowned "mark attendance" task explains stale marking better than
    any adjective. Narrowed to one stage because a program carries dozens of task
    rows and a prompt full of closed acquisition tasks is noise, not context."""
    return tuple(
        task
        for task in tasks
        if task.stage is ProgramStage.ACTIVE_MONITORING and task.status is not TaskStatus.DONE
    )


def briefing_payload(
    assessment: RiskAssessment,
    coverage: Sequence[SignalCoverage],
    *,
    program: ProgramSnapshot | None = None,
    open_tasks: Sequence[TaskSnapshot] = (),
    recipients: Sequence[ContactSnapshot] = (),
    escalations: EscalationDecision | None = None,
) -> dict[str, JsonValue]:
    """Everything the briefing may state, and nothing else. Pure; no model.

    Built and asserted separately from the agent so the R1 surface — literally
    "which values is the model allowed to quote" — can be tested without an LLM.

    `Decimal` values are stringified rather than passed through as numbers, for
    the reason `app.agents.sourcing.ProfileRanking.as_payload` gives: the model
    must quote a score exactly, and a score arriving as a float expansion is both
    wrong to quote and ungroundable against the value held here.
    """
    return {
        "program": _program_payload(program),
        "risk": {
            "score": str(assessment.score),
            "band": assessment.band.value,
            "program_type": assessment.program_type.value,
            "observed_at": assessment.as_of.isoformat(),
            "anomaly_count": len(assessment.anomalies),
        },
        "anomalies": [
            {
                "code": anomaly.code.value,
                "severity": anomaly.severity.value,
                "message": anomaly.message,
                "detail": dict(anomaly.detail),
            }
            for anomaly in assessment.anomalies
        ],
        "signal_coverage": [
            {"family": row.family.value, "coverage": row.coverage.value} for row in coverage
        ],
        "not_measured": [row.family.value for row in coverage if not row.is_measured],
        "open_monitoring_tasks": [
            {
                "title": task.title,
                "status": task.status.value,
                "due_on": task.due_on.isoformat() if task.due_on else None,
                "owner": task.owner.display_name if task.owner else None,
            }
            for task in open_tasks
        ],
        "internal_staff_on_program": [contact.display_name for contact in recipients],
        "escalations": _escalation_payload(escalations),
    }


def _program_payload(program: ProgramSnapshot | None) -> JsonValue:
    """The program snapshot as JSON, or `None` when it was out of reach (§4 RLS).

    `None` is reported rather than papered over. A briefing about a program the
    caller cannot read is a briefing whose college name would otherwise be
    guessed, and guessing it is the R1 breach this whole layer exists to prevent.
    """
    if program is None:
        return None
    return {
        "college_name": program.college_name,
        "program_type": program.program_type,
        "stage": program.stage.value,
        "starts_on": program.starts_on.isoformat() if program.starts_on else None,
        "ends_on": program.ends_on.isoformat() if program.ends_on else None,
    }


def _escalation_payload(decision: EscalationDecision | None) -> JsonValue:
    """What the deterministic SLA engine decided, verbatim.

    `reason` is `SlaRule.explain()` output — generated from the rule row itself,
    not written about it. The model is handed that sentence to paraphrase and is
    given no way to reach the rule table, so it cannot decide that something
    should have escalated when the engine said it should not (§8: "deterministic
    SLA rules. Not LLM judgement").
    """
    if decision is None:
        return None
    highest = decision.highest_tier
    return {
        "fired": decision.fired,
        "highest_tier": highest.value if highest is not None else None,
        "rules": [
            {
                "code": escalation.code.value,
                "metric": escalation.metric.value,
                "measured": str(escalation.measured),
                "threshold": str(escalation.threshold),
                "tier": escalation.tier.value,
                "severity": escalation.severity.value,
                "reason": escalation.reason,
            }
            for escalation in decision.escalations
        ],
    }


# --- the agent ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonitorBriefing:
    """A deterministic alert and the prose written about it, kept apart.

    The separation is the same one `app.agents.ports.Draft` makes between
    `payload` and `body`, and it is what keeps the model from becoming the source
    of the reading: `alert.headline` and `alert.lines` are rendered by
    `build_alert()` from the assessment and are what a dashboard shows as the
    authoritative risk statement. `narrative` is explanation beside it, never
    instead of it.

    There is no `SavedDraft` here and there cannot be one. The Delivery Monitor
    holds no write capability at all (R3, §8), so its output is a value the
    caller renders — the same shape the supervisor's assessment takes.
    """

    assessment: RiskAssessment
    alert: InternalAlert
    coverage: tuple[SignalCoverage, ...]
    narrative: str
    invocation: AgentInvocation
    grounded_in: Mapping[str, JsonValue]

    @property
    def headline(self) -> str:
        """The deterministic reading. Not model output."""
        return self.alert.headline

    @property
    def unmeasured(self) -> tuple[SignalFamily, ...]:
        """Families nobody had a reading for. Not families that were fine."""
        return tuple(row.family for row in self.coverage if not row.is_measured)


@dataclass
class MonitorAgent:
    """Explains a computed risk assessment to internal staff. Level 1, Observe.

    Construction refuses three things, each naming the rule it protects: the
    wrong runtime, an autonomy level above §8's ceiling, and — the one worth
    reading twice — a toolset that can write. The third is unreachable today
    because `MONITOR_TOOLS` binds three read tools and no `save_draft`, which
    makes it a live tripwire rather than dead code: if somebody grants this agent
    a write capability in `catalog.py`, every monitor in the system fails to
    build instead of quietly gaining one.
    """

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.MONITOR:
            raise ValueError(
                f"MonitorAgent needs the monitor runtime, got '{self.runtime.agent.value}'"
            )
        if self.runtime.autonomy > AutonomyLevel.OBSERVE:
            raise ValueError(
                "The Delivery Monitor's ceiling is Alert, internal only (CLAUDE.md §8) — "
                "autonomy level 1, Observe. It reads, reports and alerts internally; it "
                "drafts nothing and it sends nothing."
            )
        if self.runtime.dispatcher.toolset.can_write:
            raise ValueError(
                "The Delivery Monitor was handed a toolset with a write capability. Its "
                "ceiling is Alert (CLAUDE.md §8) and R3 binds capability to the toolset, "
                "so MONITOR_TOOLS holds read tools only and must continue to."
            )

    async def explain_alert(
        self,
        assessment: RiskAssessment,
        ctx: MonitorContext,
        *,
        audience: Sequence[Persona],
        escalations: EscalationDecision | None = None,
    ) -> MonitorBriefing:
        """Write the internal briefing for one already-computed assessment.

        `assessment` comes from `app.services.monitoring.assess_risk(ctx)` and
        `escalations` from `app.services.escalation.evaluate_sla()`. Both are
        computed by the caller, before this method is entered, and neither is
        recomputed, adjusted or overridden here.

        `ctx` is required alongside the assessment even though the assessment was
        derived from it, because the assessment records what *fired* and only the
        context records what was *looked at*. Without it the briefing could not
        distinguish a clean usage signal from no usage signal, which is the
        distinction §14 Q6 makes load-bearing today.

        Raises `ValueError` if the two describe different programs, or if the
        audience contains an external persona (`build_alert` refuses it);
        `UngroundedFigureError` if the model states a figure it was not given;
        `FabricatedAnomalyError` if it names a finding that did not fire. None of
        the three is retried — a retry turns a systematic grounding problem into
        an intermittent one and hides the signal that the model is wrong.
        """
        if ctx.program_id != assessment.program_id:
            raise ValueError(
                f"MonitorContext is for program {ctx.program_id} but the RiskAssessment is "
                f"for {assessment.program_id}. Reporting one program's anomalies under "
                "another program's name is the R1 breach this layer exists to prevent."
            )
        if escalations is not None and escalations.facts.program_id != assessment.program_id:
            raise ValueError(
                f"EscalationDecision is for program {escalations.facts.program_id} but the "
                f"RiskAssessment is for {assessment.program_id}."
            )

        # Deterministic first, and it refuses an external audience before a single
        # row is read (§8: Alert, INTERNAL ONLY).
        alert = build_alert(assessment, tuple(audience))
        coverage = signal_coverage(ctx)

        program = await self.runtime.dispatcher.read_program(assessment.program_id)
        tasks = await self.runtime.dispatcher.list_program_tasks(assessment.program_id)
        contacts = _internal_only(
            await self.runtime.dispatcher.list_internal_contacts(assessment.program_id)
        )

        grounded_in = briefing_payload(
            assessment,
            coverage,
            program=program,
            open_tasks=_open_monitoring_tasks(tasks),
            recipients=contacts,
            escalations=escalations,
        )
        body, invocation = await self.runtime.generate(
            LLMTask.SUMMARY,
            system=_BRIEFING_SYSTEM,
            user=(
                "DELIVERY OBSERVATION, already detected, scored and banded. Explain it; "
                "change nothing. Families listed under `not_measured` were not looked at "
                "in this run and must not be described as healthy:\n"
                f"{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="monitor.briefing",
        )
        # Grounding caught invented figures. This catches an invented finding,
        # which carries no digits at all.
        assert_only_detected_anomalies(
            body, [anomaly.code for anomaly in assessment.anomalies], "monitor.briefing"
        )

        _log.info(
            "monitor.briefed",
            program_id=str(assessment.program_id),
            band=assessment.band.value,
            score=str(assessment.score),
            anomalies=len(assessment.anomalies),
            not_measured=[family.value for family in _unmeasured(coverage)],
            escalations=len(escalations.escalations) if escalations else 0,
        )
        return MonitorBriefing(
            assessment=assessment,
            alert=alert,
            coverage=coverage,
            narrative=body,
            invocation=invocation,
            grounded_in=grounded_in,
        )


def _unmeasured(coverage: Sequence[SignalCoverage]) -> tuple[SignalFamily, ...]:
    return tuple(row.family for row in coverage if not row.is_measured)
