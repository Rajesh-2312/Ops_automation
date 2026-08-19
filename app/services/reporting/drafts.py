"""Binding a report to the R4 lifecycle. CLAUDE.md R4, §8 (ceiling: Draft).

    R4  "Every artifact moves DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED."
    §8  Reporting's ceiling is Draft: "propose, human edits and sends".

This module is the join between the facts (`assembly.py`), the prose
(`narration.py`) and the lifecycle machinery that already exists in
`app.services.approval`. It creates artifacts in DRAFT and nothing else. There is
no `submit`, no `approve`, no `release` here and there must never be: those are
`app/api/approvals.py`'s, behind an authenticated human session, and importing
them into a drafting module is how an agent path to release gets built by
accident.

WHY THE NARRATIVE IS INSIDE THE FROZEN PAYLOAD
==============================================
`approve()` hashes the payload, and `release()` refuses if the payload has moved
since. The prose is the part of a governance report a college actually reads, so
leaving it outside the hash would let the sentences be rewritten after approval
while the artifact still evidenced an approval — the exact failure the freeze
exists to prevent. Facts and prose stay in separate keys, per R1's division, but
both are inside the freeze.

GOVERNANCE REPORTS CANNOT CURRENTLY BE APPROVED, AND THAT IS CORRECT
====================================================================
`APPROVAL_AUTHORITY` in `app/domain/enums.py` has no entry for
`GOVERNANCE_REPORT`, so `approver_personas()` raises
`ApprovalAuthorityUndefinedError`. That is not a bug and this module does not work
around it. §14 Q3 — "Approval authority for college-facing comms: Manager or
Senior Manager?" — is an open question, and §14 says carry it, do not invent an
answer.

What this module does instead is surface it honestly and early.
`approval_readiness()` reports, at drafting time, that the artifact cannot yet be
approved and names the question that has to be answered by a human. A draft is
still worth producing — someone has to read it, and the answer to Q3 will arrive
from a conversation, not from code — but nobody should discover the block for the
first time when they click Approve.

To fix it: answer Q3 and add the entry to `APPROVAL_AUTHORITY`. Not here, and not
by picking whichever persona makes a test pass.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from pydantic import JsonValue

from app.core.audit import AuditEvent
from app.domain.enums import ArtifactState, ArtifactType, Persona
from app.services.approval.state_machine import (
    ApprovalAuthorityUndefinedError,
    Artifact,
    approver_personas,
)
from app.services.reporting.assembly import CollegeSummary, GovernanceReport
from app.services.reporting.narration import Narration

__all__ = [
    "ApprovalReadiness",
    "ReportAction",
    "ReportDraft",
    "approval_readiness",
    "draft_college_summary",
    "draft_governance_report",
]


class ReportAction:
    """Audit vocabulary for Phase 6.

    Plain constants rather than a `StrEnum`, for the reason `app.core.audit`
    documents: `AuditEvent.action` is typed `str` precisely so a workstream can
    name its own actions without editing a shared file.

    One action, and it is a drafting action. There is no `report.shared`, no
    `report.sent` — `governance_reports.shared_with_college_at` is set by whoever
    releases, under a human session, and nothing in this package can reach it.
    """

    DRAFTED: Final[str] = "report.drafted"


@dataclass(frozen=True, slots=True)
class ApprovalReadiness:
    """Whether this artifact type can be approved at all, and by whom.

    Reported alongside every draft rather than discovered at the approval attempt.
    `blocked_reason` is written for the person holding the draft, and it names §14
    Q3 explicitly so the next question ("who do I ask?") has an answer.
    """

    artifact_type: ArtifactType
    can_be_approved: bool
    approvers: tuple[Persona, ...] = ()
    blocked_reason: str | None = None

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "artifact_type": self.artifact_type.value,
            "can_be_approved": self.can_be_approved,
            "approvers": [p.value for p in self.approvers],
            "blocked_reason": self.blocked_reason,
        }


def approval_readiness(artifact_type: ArtifactType) -> ApprovalReadiness:
    """Ask the approval layer who may approve this type, without raising.

    The raise from `approver_personas()` is the right behaviour AT the approval
    attempt — loud, naming the open question. It is the wrong behaviour when a
    Manager is merely generating a draft to read, so it is caught here and turned
    into a reported state. The state machine keeps its refusal; this is a query
    about it, not a bypass of it.
    """
    try:
        permitted = approver_personas(artifact_type)
    except ApprovalAuthorityUndefinedError as exc:
        return ApprovalReadiness(
            artifact_type=artifact_type,
            can_be_approved=False,
            blocked_reason=str(exc),
        )
    return ApprovalReadiness(
        artifact_type=artifact_type,
        can_be_approved=True,
        approvers=tuple(sorted(permitted, key=lambda p: p.value)),
    )


@dataclass(frozen=True, slots=True)
class ReportDraft:
    """A DRAFT artifact, its narrative, its audit row, and its approval state.

    The four travel together for the reason `ApprovalOutcome` gives in
    `app.services.approval.state_machine`: returning the artifact without its
    audit event makes it possible to persist one and forget the other, and §11
    does not treat the audit row as optional.
    """

    artifact: Artifact
    event: AuditEvent
    approval: ApprovalReadiness
    narrative: Narration | None = None

    def __post_init__(self) -> None:
        if self.artifact.state is not ArtifactState.DRAFT:
            raise ValueError(
                f"a report draft was constructed in {self.artifact.state.value}. Phase 6's "
                "ceiling is Draft (CLAUDE.md §8) and this package has no transition of any "
                "kind — DRAFT is the only state it may produce."
            )


def draft_governance_report(
    report: GovernanceReport,
    *,
    artifact_id: UUID,
    actor_id: UUID | None = None,
    actor_persona: Persona | None = None,
    narrative: Narration | None = None,
    at: dt.datetime | None = None,
) -> ReportDraft:
    """Build the DRAFT governance-report artifact. Writes nothing.

    Pure, like the approval state machine it feeds: it returns the artifact and
    the `AuditEvent` describing its creation, and persisting both in one
    transaction is the caller's job (§11).

    `actor_id` may be `None` — §11 allows a NULL actor "only for a scheduled job
    with no human behind it", which a nightly report run is. When a human asked
    for the report their id is passed through so the trail says who.
    """
    payload = _payload(report.as_payload(), narrative)
    artifact = Artifact(
        artifact_type=ArtifactType.GOVERNANCE_REPORT,
        artifact_id=artifact_id,
        version=1,
        state=ArtifactState.DRAFT,
        payload=payload,
    )
    approval = approval_readiness(ArtifactType.GOVERNANCE_REPORT)
    event = AuditEvent(
        actor_id=actor_id,
        actor_persona=actor_persona,
        action=ReportAction.DRAFTED,
        entity_table=ArtifactType.GOVERNANCE_REPORT.value,
        entity_id=artifact_id,
        before=None,
        after=_snapshot(artifact, narrative, approval)
        | {
            "program_id": str(report.program.program_id),
            "period_start": report.period.start.isoformat(),
            "period_end": report.period.end.isoformat(),
            # §4: a report carrying trainer cost is a commercial document, and the
            # audit trail should say which kind was produced without anyone having
            # to re-read the payload.
            "is_commercial": report.is_commercial,
        },
        at=at if at is not None else dt.datetime.now(dt.UTC),
    )
    return ReportDraft(artifact=artifact, event=event, approval=approval, narrative=narrative)


def draft_college_summary(
    summary: CollegeSummary,
    *,
    artifact_id: UUID,
    actor_id: UUID | None = None,
    actor_persona: Persona | None = None,
    narrative: Narration | None = None,
    at: dt.datetime | None = None,
) -> ReportDraft:
    """Build the DRAFT college-summary artifact.

    Typed as `PROGRAM_DOCUMENT` rather than `GOVERNANCE_REPORT`: a college summary
    is not the periodic governance report, and `ArtifactType`'s value doubles as
    the `entity_table` on the audit row (§11), so calling it one would file it
    against `governance_reports` where nobody would find it. `PROGRAM_DOCUMENT`
    has no `APPROVAL_AUTHORITY` entry either, so the same §14 Q3 block is reported
    — correctly, since a summary shared with a college is exactly the
    college-facing communication Q3 is about.
    """
    artifact = Artifact(
        artifact_type=ArtifactType.PROGRAM_DOCUMENT,
        artifact_id=artifact_id,
        version=1,
        state=ArtifactState.DRAFT,
        payload=_payload(summary.as_payload(), narrative),
    )
    approval = approval_readiness(ArtifactType.PROGRAM_DOCUMENT)
    event = AuditEvent(
        actor_id=actor_id,
        actor_persona=actor_persona,
        action=ReportAction.DRAFTED,
        entity_table=ArtifactType.PROGRAM_DOCUMENT.value,
        entity_id=artifact_id,
        before=None,
        after=_snapshot(artifact, narrative, approval)
        | {"college_id": str(summary.college_id), "is_commercial": False},
        at=at if at is not None else dt.datetime.now(dt.UTC),
    )
    return ReportDraft(artifact=artifact, event=event, approval=approval, narrative=narrative)


def _payload(facts: dict[str, JsonValue], narrative: Narration | None) -> dict[str, JsonValue]:
    """Facts and prose in one frozen payload, in separate keys.

    Separate keys because R1 separates them — "the database owns truth, the LLM
    owns language", and a reviewer must be able to tell at a glance which half
    they are reading. One payload because R4 freezes one payload, and prose
    outside the freeze is prose that can be rewritten after approval.
    """
    payload: dict[str, JsonValue] = dict(facts)
    payload["narrative"] = narrative.body if narrative else None
    payload["narrative_model"] = narrative.model if narrative else None
    return payload


def _snapshot(
    artifact: Artifact, narrative: Narration | None, approval: ApprovalReadiness
) -> dict[str, JsonValue]:
    """The audit row's view of a new draft. §11: actor, action, before, after, at.

    The body is fingerprinted by length and by the payload hash rather than
    copied. An audit row is not a content store, and a drafted college-facing
    document duplicated into `audit_events` would be reviewable content living
    outside the artifact's own access rules (§4) — the same argument
    `app.services.approval.state_machine._snapshot()` makes.
    """
    return {
        "state": artifact.state.value,
        "version": artifact.version,
        "payload_hash": artifact.payload_hash(),
        "narrative_chars": len(narrative.body) if narrative else 0,
        "narrative_model": narrative.model if narrative else None,
        "llm_task": narrative.task.value if narrative else None,
        "approval": approval.as_payload(),
    }
