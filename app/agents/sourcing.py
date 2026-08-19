"""Sourcing Liaison. CLAUDE.md §8: "Requirement spec, TA follow-up, re-spec
diffs, profile ranking". Ceiling: Draft.

THE DIVISION OF LABOUR THIS MODULE IS BUILT AROUND
==================================================
Three of this agent's four jobs are arithmetic or set operations wearing a
judgement costume, and the fourth is writing. R1 draws the line — "the database
owns truth, the LLM owns language" — and here it falls in a specific and
load-bearing place:

* **Profile ranking is pure Python.** `rank_profiles()` computes a score from
  skill overlap and years of experience, in `Decimal`, deterministically, with no
  model involved. A ranking is an ordering people act on: a trainer is contacted
  or is not. An LLM ranking would reorder between runs for no stated reason, and
  "why was I ranked fourth?" would have no answer. The model's job is to *explain*
  the ordering it was handed, which is R2's shape ("an agent may explain a number,
  it may never produce one") applied to something that is not money.

* **The re-spec diff is a set operation.** `diff_spec()` compares two mappings.
  Asking a model which fields changed is asking it to invent a fact that `!=`
  already knows, and it would occasionally miss one.

* **The requirement spec and the TA follow-up are prose.** Those go to the model,
  on the volume tier (§2: drafting and chase), grounded against the structured
  input, and come back as drafts a human edits and sends.

NOBODY IS CONTACTED
===================
"TA follow-up" is a *draft* follow-up. This agent holds `save_draft` and no send
tool (R3), so the chase message lands in DRAFT for a human. `_internal_only()`
additionally restricts who a follow-up may even be addressed to: TA and Sourcing
are internal actors (§4), and a follow-up drafted at a trainer or a college
contact would be an external communication drafted by an agent whose ceiling does
not contemplate one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

import structlog
from pydantic import JsonValue

from app.agents.ports import ContactSnapshot, Draft, ProgramSnapshot, TrainerProfileSnapshot
from app.agents.runtime import AgentRuntime, DraftOutcome
from app.agents.tools.catalog import AgentName
from app.domain.enums import (
    INTERNAL_PERSONAS,
    ArtifactType,
    AutonomyLevel,
    LLMTask,
    Persona,
    ProgramStage,
)

__all__ = [
    "ProfileRanking",
    "SourcingAgent",
    "SpecDiff",
    "diff_spec",
    "rank_profiles",
]

_log = structlog.get_logger(__name__)

#: Weight of skill overlap against experience in the ranking score. Stated as a
#: constant with `Decimal` literals so the trade-off is visible and arguable
#: rather than buried in an expression — and so it can be tuned without anybody
#: having to re-derive what the formula meant.
_SKILL_WEIGHT: Final[Decimal] = Decimal("0.75")
_EXPERIENCE_WEIGHT: Final[Decimal] = Decimal("0.25")
#: Experience is scored against this ceiling, so ten years and twenty years do not
#: separate a candidate the way two years and six do. Beyond it, more years stop
#: being the discriminator that matters for a training deployment.
_EXPERIENCE_CEILING: Final[Decimal] = Decimal("8")

_SPEC_SYSTEM: Final[str] = (
    "You draft trainer requirement specifications for byteXL's TA team.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the counts, dates, durations and skill names in the structured data "
    "given to you. State no number that is not there.\n"
    "2. Do not compute totals, head-counts, durations or rates.\n"
    "3. Write for a recruiter: what skills, what level, where, when, how many.\n"
    "4. Mark anything the structured data leaves unstated as 'to be confirmed'. Do not "
    "fill it in."
)

_FOLLOWUP_SYSTEM: Final[str] = (
    "You draft short internal follow-up messages from byteXL operations to the internal "
    "TA/Sourcing team about an open trainer requirement.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the figures, dates and names in the structured data given to you.\n"
    "2. Colleagues, not customers: direct, no apologising, no marketing tone.\n"
    "3. Say what is needed and by when, and name what is blocking.\n"
    "4. Six sentences at most.\n"
    "5. A human will read, edit and send this. Do not write as if it has been sent."
)

_RATIONALE_SYSTEM: Final[str] = (
    "You explain a trainer shortlist to a byteXL operations manager.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. The ranking and every score are given to you. Explain them; do not re-rank, "
    "re-score, or compute anything.\n"
    "2. Use ONLY the numbers in the structured data. Quote a score exactly as given.\n"
    "3. One sentence per candidate, in the order given.\n"
    "4. Name what each candidate is missing against the requirement, if anything."
)


# --- deterministic: ranking and diffs ---------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileRanking:
    """One candidate's position, computed in Python and explained by the model.

    `score` is a `Decimal` in [0, 1] quantised to three places. Quantised once, at
    the end, and never fed back into another calculation — R6's discipline applied
    outside money, because a ranking that changes on re-computation is as
    confusing as a payout that does.
    """

    profile: TrainerProfileSnapshot
    score: Decimal
    matched_skills: tuple[str, ...]
    missing_skills: tuple[str, ...]

    def as_payload(self) -> dict[str, JsonValue]:
        """The ranking as structured input for the rationale prompt.

        `score` is stringified rather than passed as a float: the model must quote
        it exactly, and `0.75` arriving as `0.7500000000000000444` is both wrong
        to quote and ungroundable against the value stored here (R7's reasoning,
        applied to a number that is not money but is still compared for equality).
        """
        return {
            "name": self.profile.display_name,
            "score": str(self.score),
            "matched_skills": list(self.matched_skills),
            "missing_skills": list(self.missing_skills),
            "years_experience": (
                str(self.profile.years_experience)
                if self.profile.years_experience is not None
                else None
            ),
        }


def rank_profiles(
    profiles: Sequence[TrainerProfileSnapshot], required_skills: Sequence[str]
) -> tuple[ProfileRanking, ...]:
    """Rank candidates against a requirement. Pure, deterministic, no LLM.

    Score is a weighted sum of skill coverage and capped experience. Skill
    matching is case-insensitive and whitespace-trimmed but otherwise exact —
    fuzzy matching would make "Java" match "JavaScript", which is the single most
    expensive false positive in this domain.

    Ties break on name, not on input order. Two candidates with identical scores
    must not swap places because a database returned rows in a different order;
    somebody will notice, and "the ranking changed but nothing changed" costs more
    trust than the ordering is worth.

    With no required skills every candidate scores on experience alone, which is
    the honest answer to "rank these against nothing" rather than a refusal — the
    caller sees a spec-less requirement in the empty `matched_skills`.
    """
    required = tuple(
        dict.fromkeys(skill.strip().lower() for skill in required_skills if skill.strip())
    )
    rankings: list[ProfileRanking] = []
    for profile in profiles:
        held = {skill.strip().lower(): skill for skill in profile.skills if skill.strip()}
        matched = tuple(held[skill] for skill in required if skill in held)
        missing = tuple(skill for skill in required if skill not in held)

        coverage = Decimal(len(matched)) / Decimal(len(required)) if required else Decimal(0)
        years = profile.years_experience or Decimal(0)
        experience = min(years, _EXPERIENCE_CEILING) / _EXPERIENCE_CEILING

        raw = coverage * _SKILL_WEIGHT + experience * _EXPERIENCE_WEIGHT if required else experience
        rankings.append(
            ProfileRanking(
                profile=profile,
                score=raw.quantize(Decimal("0.001")),
                matched_skills=matched,
                missing_skills=missing,
            )
        )
    rankings.sort(key=lambda ranking: (-ranking.score, ranking.profile.display_name))
    return tuple(rankings)


@dataclass(frozen=True, slots=True)
class SpecDiff:
    """What changed between two requirement specs. §8's "re-spec diffs".

    Three buckets, and `changed` carries both values. A re-spec that says "the
    skills changed" without saying from what to what makes TA re-read both
    documents, which is the work the diff exists to remove.
    """

    added: Mapping[str, JsonValue]
    removed: Mapping[str, JsonValue]
    changed: Mapping[str, tuple[JsonValue, JsonValue]]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "added": dict(self.added),
            "removed": dict(self.removed),
            "changed": {key: [old, new] for key, (old, new) in self.changed.items()},
        }


def diff_spec(
    current: Mapping[str, JsonValue] | None, proposed: Mapping[str, JsonValue]
) -> SpecDiff:
    """Compare two requirement specs. A set operation, not a judgement.

    `current` is `None` on a first spec, in which case everything is `added` —
    which reads correctly on a dashboard and needs no special case at the call
    site.
    """
    existing = dict(current or {})
    added = {key: value for key, value in proposed.items() if key not in existing}
    removed = {key: value for key, value in existing.items() if key not in proposed}
    changed = {
        key: (existing[key], value)
        for key, value in proposed.items()
        if key in existing and existing[key] != value
    }
    return SpecDiff(added=added, removed=removed, changed=changed)


def _internal_only(contacts: Sequence[ContactSnapshot]) -> tuple[ContactSnapshot, ...]:
    """Drop external parties. §4's internal personas; §8's ceiling on this agent.

    TA and Sourcing are internal actors. A follow-up addressed at a trainer or a
    college would be an external communication, which no draft agent in Phase 4
    contemplates and which R3 gives nothing the capability to deliver anyway.
    """
    return tuple(contact for contact in contacts if contact.persona in INTERNAL_PERSONAS)


# --- the agent ---------------------------------------------------------------


@dataclass
class SourcingAgent:
    """Drafts specs, follow-ups and shortlist rationales. Level 2, Draft (§8)."""

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.SOURCING:
            raise ValueError(
                f"SourcingAgent needs the sourcing runtime, got '{self.runtime.agent.value}'"
            )
        if self.runtime.autonomy > AutonomyLevel.DRAFT:
            raise ValueError(
                "Sourcing Liaison's ceiling is Draft (CLAUDE.md §8). It proposes a spec and "
                "a follow-up; a human sends them."
            )

    async def draft_requirement_spec(
        self,
        program_id: UUID,
        proposed: Mapping[str, JsonValue],
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Draft a requirement spec, with the re-spec diff against the current one.

        The diff is computed before the model is called and passed in as
        structured input, so the prose describes a diff that was calculated rather
        than one that was noticed.
        """
        program = await self.runtime.dispatcher.read_program(program_id)
        current = await self.runtime.dispatcher.read_requirement_spec(program_id)
        diff = diff_spec(current, proposed)

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            "proposed_spec": dict(proposed),
            "diff": diff.as_payload(),
            "is_respec": current is not None,
        }
        body, invocation = await self.runtime.generate(
            LLMTask.DRAFTING,
            system=_SPEC_SYSTEM,
            user=(
                "REQUIREMENT SPEC (the only figures, dates and skill names you may "
                f"state):\n{json.dumps(grounded_in, indent=2, default=str)}\n\n"
                + (
                    "This is a RE-SPEC. Lead with what changed, using the `diff` block."
                    if current is not None
                    else "This is the first spec for this requirement."
                )
            ),
            structured_input=grounded_in,
            context="sourcing.requirement_spec",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Trainer requirement spec — {_college_of(program)}",
            body=body,
            payload={"spec": dict(proposed), "diff": diff.as_payload()},
            program_id=program_id,
            flags=() if not diff.is_empty or current is None else ("re-spec with no changes",),
            grounded_in=grounded_in,
        )
        _log.info(
            "sourcing.spec_drafted",
            program_id=str(program_id),
            respec=current is not None,
            added=len(diff.added),
            removed=len(diff.removed),
            changed=len(diff.changed),
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_shortlist(
        self,
        program_id: UUID,
        required_skills: Sequence[str],
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Rank the submitted profiles and draft the rationale.

        The ordering and every score are computed by `rank_profiles()` before the
        model sees anything. The model receives the finished ranking and writes
        one sentence per candidate. If it states a score that is not in the
        ranking, `assert_grounded` refuses the draft — which is the check that
        makes "explain, do not produce" enforceable rather than instructed.
        """
        profiles = await self.runtime.dispatcher.list_candidate_profiles(program_id)
        rankings = rank_profiles(profiles, required_skills)

        grounded_in: dict[str, JsonValue] = {
            "required_skills": list(required_skills),
            "ranking": [ranking.as_payload() for ranking in rankings],
            "candidate_count": len(rankings),
        }
        body, invocation = await self.runtime.generate(
            LLMTask.DRAFTING,
            system=_RATIONALE_SYSTEM,
            user=(
                "SHORTLIST, already ranked and scored. Explain it in the order given:\n"
                f"{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="sourcing.shortlist",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Trainer shortlist — {len(rankings)} profile(s)",
            body=body,
            payload={"ranking": [ranking.as_payload() for ranking in rankings]},
            program_id=program_id,
            flags=() if rankings else ("no profiles submitted against this requirement",),
            grounded_in=grounded_in,
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )

    async def draft_ta_followup(
        self,
        program_id: UUID,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> DraftOutcome:
        """Draft an internal chase to TA about an open requirement. Nobody sends it.

        Recipients are filtered to internal personas before the model is called,
        so an external contact is not even in the prompt. Combined with R3 — this
        agent has no send tool — the follow-up is a draft addressed to a colleague,
        which is as far as §8's Draft ceiling goes.
        """
        program = await self.runtime.dispatcher.read_program(program_id)
        tasks = await self.runtime.dispatcher.list_program_tasks(program_id)
        contacts = _internal_only(await self.runtime.dispatcher.list_internal_contacts(program_id))
        profiles = await self.runtime.dispatcher.list_candidate_profiles(program_id)
        spec = await self.runtime.dispatcher.read_requirement_spec(program_id)

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            "requirement_spec": dict(spec) if spec else None,
            "profiles_submitted": len(profiles),
            "open_sourcing_tasks": [
                {
                    "title": task.title,
                    "status": task.status.value,
                    "due_on": task.due_on.isoformat() if task.due_on else None,
                }
                for task in tasks
                if task.stage is ProgramStage.TRAINER_SOURCING
            ],
            "recipients": [contact.display_name for contact in contacts],
        }
        body, invocation = await self.runtime.generate(
            LLMTask.CHASE,
            system=_FOLLOWUP_SYSTEM,
            user=(
                "OPEN REQUIREMENT (the only figures, dates and names you may state):\n"
                f"{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="sourcing.ta_followup",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"TA follow-up — {_college_of(program)}",
            body=body,
            payload={"recipients": [contact.display_name for contact in contacts]},
            program_id=program_id,
            flags=() if contacts else ("no internal recipient identified — assign before sending",),
            grounded_in=grounded_in,
        )
        return await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )


def _program_payload(program: ProgramSnapshot | None) -> JsonValue:
    """The program snapshot as JSON, or `None` when it was out of reach (§4 RLS)."""
    if program is None:
        return None
    return {
        "college_name": program.college_name,
        "program_type": program.program_type,
        "stage": program.stage.value,
        "starts_on": program.starts_on.isoformat() if program.starts_on else None,
        "ends_on": program.ends_on.isoformat() if program.ends_on else None,
    }


def _college_of(program: ProgramSnapshot | None) -> str:
    return program.college_name if program is not None else "unknown program"
