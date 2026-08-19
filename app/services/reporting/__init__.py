"""Phase 6 Reporting (CLAUDE.md §8). Governance reports, feedback synthesis,
college summaries. Ceiling: **Draft** — it proposes, a human edits and sends.

Three modules, split along CLAUDE.md R1's line:

    assembly.py    the facts. Pure, no I/O, no model. Every figure arrives as
                   structured input the caller read from SQL.
    narration.py   the prose. The sole gateway (§2), grounded against the facts
                   with `app.agents.grounding` and cited with `app.rag.guards`.
    drafts.py      the R4 binding. Creates DRAFT artifacts and their audit rows,
                   and nothing else — no submit, no approve, no release.

Nothing in this package transmits anything, and nothing in it computes money:
figures on the commercial side are read back from `remuneration_sheets` rows the
engine wrote (R2), never recomputed and never totalled here.
"""

from app.services.reporting.assembly import (
    AssessmentFacts,
    BatchFacts,
    CollegeSummary,
    DeliverySection,
    FeedbackEntry,
    FeedbackSynthesis,
    GovernanceReport,
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
from app.services.reporting.drafts import (
    ApprovalReadiness,
    ReportAction,
    ReportDraft,
    approval_readiness,
    draft_college_summary,
    draft_governance_report,
)
from app.services.reporting.narration import (
    Citation,
    Completer,
    Narration,
    NarrationRefused,
    ReportNarrator,
)

__all__ = [
    "ApprovalReadiness",
    "AssessmentFacts",
    "BatchFacts",
    "Citation",
    "CollegeSummary",
    "Completer",
    "DeliverySection",
    "FeedbackEntry",
    "FeedbackSynthesis",
    "GovernanceReport",
    "Narration",
    "NarrationRefused",
    "ProgramFacts",
    "ProgramSummaryLine",
    "ReportAction",
    "ReportDraft",
    "ReportNarrator",
    "ReportPeriod",
    "StudentAttendanceFacts",
    "TaskFacts",
    "TrainerCostLine",
    "TrainerCostSection",
    "TrainerDeliveryFacts",
    "approval_readiness",
    "assemble_governance_report",
    "draft_college_summary",
    "draft_governance_report",
    "summarise_college",
    "synthesise_feedback",
]
