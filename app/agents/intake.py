"""Intake agent. CLAUDE.md §8: "Parse MoU/PO/mail -> structured Program draft,
flag unusual clauses". Ceiling: Draft.

WHAT THIS AGENT IS ALLOWED TO DO
================================
It reads a source document — an MoU, a PO, a forwarded mail thread — and proposes
a Program record for a human to correct and accept. It never creates a Program.
Autonomy level 2 (§8): "propose, human edits and sends". The output is a `Draft`,
saved through the one write capability the layer has, and it sits in DRAFT until
a human moves it (R4).

R1 UNDER EXTRACTION, WHICH IS THE INTERESTING CASE
==================================================
R1 says an agent may not assert a fact it did not read from a system of record.
Extraction looks like an exception — the model is *producing* field values — but
it is not, and the distinction is worth stating precisely: **the source document
is the record**, and every value extracted must be present in it. So this agent
grounds twice, against different inputs, in this order:

1. The extracted payload is checked against the **source document text**. A
   contract value of ₹12,00,000 that appears nowhere in the MoU is a
   hallucinated commercial term, and it would otherwise arrive as a
   structured-looking field that a reviewer trusts more than prose. This is the
   check that matters most and it is the one most systems skip.
2. The prose summary and the clause flags are checked against the payload *and*
   the document. Language about facts, where the facts came from the record.

A failure at either step raises `UngroundedFigureError`. There is no retry loop
here. A retry converts a systematic grounding problem into an intermittent one
and hides the signal that the prompt or the model is wrong; the caller sees the
failure and a human sees a program that did not get parsed, which is a visible,
fixable state.

WHY EXTRACTION IS FRONTIER TIER
===============================
§2 routes "document extraction" to the frontier tier, and the drafting of prose
to the volume tier. This agent makes two calls, on two tiers, for exactly that
reason — a cheap model reading a contract is a false economy, and a frontier
model writing three sentences of summary is a real one. `LLMTask` carries the
routing; no model id appears in this file.

R2: NO MONEY IS COMPUTED HERE
=============================
The agent may extract a contract value that is written in the document. It never
adds, prorates or converts one, and nothing in `app/agents/` imports
`app.services.remuneration.engine` — a test asserts that. The commercial terms it
extracts are proposed values for a human to confirm against the paper, not
computed amounts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog
from pydantic import JsonValue

from app.agents.grounding import assert_grounded
from app.agents.parsing import parse_json_object
from app.agents.ports import Draft, RetrievedPassage
from app.agents.runtime import AgentRuntime, DraftOutcome
from app.agents.tools.catalog import AgentName
from app.domain.enums import ArtifactType, AutonomyLevel, Corpus, LLMTask, Persona

__all__ = ["INTAKE_FIELDS", "IntakeAgent", "IntakeResult"]

_log = structlog.get_logger(__name__)

#: The fields the extraction is asked for. A closed list, because an open-ended
#: "extract everything relevant" produces a different shape per document and
#: nothing downstream can rely on it. Anything the model considers important but
#: cannot fit here becomes a clause flag, which is the reviewer's problem to
#: judge — not a field the schema silently grew.
INTAKE_FIELDS: Final[tuple[str, ...]] = (
    "college_name",
    "program_type",
    "batch_count",
    "trainee_count",
    "starts_on",
    "ends_on",
    "contract_value",
    "payment_terms",
    "notice_period",
    "deliverables",
)

_EXTRACTION_SYSTEM: Final[str] = (
    "You extract structured fields from Indian college training contracts (MoU, PO, "
    "purchase order, or a mail thread confirming terms) for byteXL's operations team.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Return ONE JSON object and nothing else.\n"
    "2. Every value must appear in the source document. Copy figures and dates "
    "character-for-character as they are written. Do not convert, total, prorate or "
    "normalise any amount, count or date.\n"
    "3. If a field is not stated in the document, use null. Never guess, never infer "
    "from what is typical, and never carry a value over from another field.\n"
    "4. Put anything unusual, one-sided, ambiguous or missing into `unusual_clauses` as "
    "a list of short plain-English strings. Quote the contract wording you are "
    "describing. This list is what a human reviews first.\n"
    f"5. The object has exactly these keys: {', '.join(INTAKE_FIELDS)}, unusual_clauses."
)

_SUMMARY_SYSTEM: Final[str] = (
    "You write a short internal briefing for a byteXL operations manager who is about "
    "to review an extracted program record against the original contract.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY figures, dates and counts that appear in the structured data given to "
    "you. Do not compute totals, durations, per-day rates or anything else.\n"
    "2. If a value is null, say it is not stated. Do not estimate it.\n"
    "3. Lead with what the reviewer must check on the paper copy.\n"
    "4. Four sentences at most. No greeting, no sign-off — nobody sends this, it is "
    "read on a dashboard."
)


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """The parse, the prose, the flags, and the persisted draft.

    `extracted` is the structured record a human will edit. `outcome` carries the
    `SavedDraft`, its audit row and the §11 invocation record. Both are returned
    because the caller usually wants the payload for a form and the outcome for
    the audit trail, and fetching one back from the other would mean a read.
    """

    extracted: Mapping[str, JsonValue]
    unusual_clauses: tuple[str, ...]
    summary: str
    outcome: DraftOutcome


@dataclass
class IntakeAgent:
    """Parses a document into a proposed Program. Level 2, Draft (§8).

    Construction asserts the ceiling: `AgentRuntime.__post_init__` calls
    `require_ceiling`, so an `IntakeAgent` wired at level 3 fails to build rather
    than failing at the first release attempt.
    """

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.INTAKE:
            raise ValueError(
                f"IntakeAgent needs the intake runtime, got '{self.runtime.agent.value}'"
            )
        if self.runtime.autonomy > AutonomyLevel.DRAFT:
            raise ValueError(
                "Intake's ceiling is Draft (CLAUDE.md §8). It proposes a Program record; "
                "a human accepts it."
            )

    async def draft_program(
        self,
        *,
        document_text: str,
        source_label: str,
        program_id: UUID | None = None,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> IntakeResult:
        """Parse `document_text` into a Program draft. Two calls, two tiers.

        `source_label` names the document for the reviewer ("MoU — Malineni, Jul
        2026") and appears in the draft title. `program_id` is optional: intake
        usually runs *before* a program exists, and when it is supplied the agent
        reads the existing record and its documents so the draft can be reviewed
        as an amendment rather than as a new program.

        Raises `UngroundedFigureError` if either grounding check fails, and
        `MalformedModelOutputError` if the extraction is not a JSON object.
        Neither is retried — see the module docstring.
        """
        context = await self._read_context(program_id)

        # --- 1. Extraction. Frontier tier (§2). Grounded against the document.
        extraction_text, extraction_invocation = await self.runtime.generate(
            LLMTask.EXTRACTION,
            system=_EXTRACTION_SYSTEM,
            user=_extraction_prompt(document_text, source_label, context),
            # R1, step 1: the source document is the record; every figure the
            # model puts in a field must be in it. `context` is included because a
            # value legitimately confirmed against the existing program record is
            # also grounded.
            structured_input={"document": document_text, "context": context},
            context="intake.extract",
        )
        payload = parse_json_object(extraction_text, "intake.extract")
        clauses = _clause_list(payload.pop("unusual_clauses", None))
        extracted = {field: payload.get(field) for field in INTAKE_FIELDS}
        unexpected = sorted(set(payload) - set(INTAKE_FIELDS))
        if unexpected:
            # Not an error: a model that volunteers an extra field has usually
            # found something real. It goes to the reviewer as a flag rather than
            # into the record, because nothing downstream knows what to do with a
            # key that is not in INTAKE_FIELDS.
            clauses = (*clauses, f"model volunteered unrequested field(s): {', '.join(unexpected)}")

        # --- 2. Prose. Volume tier (§2). Grounded against the extraction.
        grounded_in: dict[str, JsonValue] = {
            "extracted": dict(extracted),
            "unusual_clauses": list(clauses),
            "context": dict(context),
        }
        summary, summary_invocation = await self.runtime.generate(
            LLMTask.DRAFTING,
            system=_SUMMARY_SYSTEM,
            user=_summary_prompt(extracted, clauses, source_label),
            structured_input=grounded_in,
            context="intake.summary",
        )
        # R1, belt and braces: the clause flags are model-authored prose too, and
        # a flag that quotes a figure not in the document is the same defect as a
        # summary that does. Checked against the document, not the extraction.
        assert_grounded("\n".join(clauses), document_text, "intake.clauses")

        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Program intake — {source_label}",
            body=summary,
            payload={"extracted": dict(extracted), "source": source_label},
            program_id=program_id,
            flags=clauses,
            grounded_in=grounded_in,
        )
        _log.info(
            "intake.drafted",
            source=source_label,
            fields_extracted=sum(1 for value in extracted.values() if value is not None),
            flags=len(clauses),
            extraction_tokens=extraction_invocation.total_tokens,
        )
        outcome = await self.runtime.save_draft(
            draft, summary_invocation, actor_id=actor_id, actor_persona=actor_persona
        )
        return IntakeResult(
            extracted=extracted,
            unusual_clauses=clauses,
            summary=summary,
            outcome=outcome,
        )

    async def _read_context(self, program_id: UUID | None) -> dict[str, JsonValue]:
        """Read what the system of record already knows. R1's half of the work.

        Every value here came from a tool call through the R3-gated dispatcher.
        The contracts corpus is searched for clause norms (§9: policy and context
        only, never figures — which is why only the citation and the passage go
        into the prompt and nothing numeric is taken from it).
        """
        if program_id is None:
            return {}
        program = await self.runtime.dispatcher.read_program(program_id)
        if program is None:
            return {}
        documents = await self.runtime.dispatcher.list_program_documents(program_id)
        norms = await self.runtime.dispatcher.search_corpus(
            Corpus.CONTRACTS.value, "standard MoU clauses, payment terms, notice period"
        )
        return {
            "existing_program": {
                "college_name": program.college_name,
                "program_type": program.program_type,
                "stage": program.stage.value,
                "starts_on": program.starts_on.isoformat() if program.starts_on else None,
                "ends_on": program.ends_on.isoformat() if program.ends_on else None,
            },
            "existing_documents": [
                {"title": document.title, "status": document.status, "signed": document.signed}
                for document in documents
            ],
            "clause_norms": _cite(norms),
        }


def _cite(passages: Sequence[RetrievedPassage]) -> list[JsonValue]:
    """Retrieved passages with their citations. §9: no citation, no answer."""
    return [
        {
            "source": f"{passage.document_title} § {passage.section}",
            "text": passage.text,
        }
        for passage in passages
    ]


def _clause_list(value: JsonValue) -> tuple[str, ...]:
    """Normalise `unusual_clauses` into a tuple of non-empty strings.

    Tolerant of a model returning a single string instead of a list — that is a
    formatting slip, not a content failure, and losing a real clause flag over it
    would be the worse outcome. Anything else is dropped rather than stringified:
    a flag a human cannot read is not a flag.
    """
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _extraction_prompt(
    document_text: str, source_label: str, context: Mapping[str, JsonValue]
) -> str:
    parts = [f"SOURCE DOCUMENT ({source_label}):", document_text.strip()]
    if context:
        parts += [
            "",
            "WHAT THE SYSTEM OF RECORD ALREADY HOLDS (for comparison — do not copy "
            "values from here into fields unless the document states them too):",
            json.dumps(context, indent=2, default=str),
        ]
    return "\n".join(parts)


def _summary_prompt(
    extracted: Mapping[str, JsonValue], clauses: Sequence[str], source_label: str
) -> str:
    return (
        f"SOURCE: {source_label}\n\n"
        "EXTRACTED RECORD (the only figures, dates and counts you may state):\n"
        f"{json.dumps(dict(extracted), indent=2, default=str)}\n\n"
        "UNUSUAL CLAUSES FLAGGED FOR REVIEW:\n"
        + ("\n".join(f"- {clause}" for clause in clauses) or "- none")
    )
