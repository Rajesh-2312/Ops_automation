"""Logistics agent. CLAUDE.md §8: "Travel need detection, booking request, onward
+ **return**". Ceiling: Draft.

THE RETURN LEG IS A TYPE, NOT AN INSTRUCTION
============================================
§8 puts "return" in bold, and it is the only word emphasised in that whole table.
A forgotten return leg is the specific, expensive, embarrassing failure this
agent exists to prevent: a trainer finishes a deployment eight hundred kilometres
from home and discovers nobody booked the way back.

A prompt that says "remember the return leg" prevents that most of the time. Most
of the time is not the standard, so the guarantee here is structural and stacks
four deep:

1. **`Itinerary` has two required fields.** `onward` and `return_leg` are both
   positional, both non-optional, both typed `TravelLeg`. A one-way itinerary is
   not a thing this module can represent, so no branch can produce one.
2. **There is no one-way constructor.** `plan_itinerary()` is the only function
   that builds an `Itinerary` and it derives both legs from the same
   `TravelRequest` in one expression. There is no `plan_onward()` to call and
   forget to follow up.
3. **`__post_init__` insists it is a round trip.** The return leg must carry the
   RETURN direction, must not travel before the onward leg, and must reverse it
   city-for-city. An "itinerary" whose return leg goes somewhere else is
   refused — a `RuntimeError`, so a broad `except ValueError` around date
   parsing cannot swallow it.
4. **The legs live in the draft's `payload`, not in its prose.** R1 already
   separates the two: `payload` is the structured content a travel desk acts on
   and `body` is the covering note. The desk therefore books from a structure
   that always has exactly two legs, whatever the model wrote. A model that
   forgets to mention the return leg produces a thin covering note, not a
   one-way trip.

The model never chooses a date, a city or a leg. It writes the sentences.

NOTHING IS BOOKED
=================
Ceiling: Draft (§8), which `__post_init__` enforces. This agent produces a
booking *request* — a draft, in DRAFT state, for a human on the travel desk to
read, edit and act on (R4). R3 gives it no send tool and none may ever be added:
`LOGISTICS_TOOLS` is three reads and `save_draft`. "Booking request" in §8 is a
noun, not a verb.

R2: NO AMOUNT APPEARS ANYWHERE HERE
===================================
There is no fare, no per-diem, no allowance and no total in this module, and the
system prompt forbids the model from stating one. Travel money is a remuneration
input — §6's `travel_reimb` and `ta_da` line items — computed in
`app/services/remuneration/engine.py` in `Decimal`. §14 Q1 is still open on
whether TA&DA is per travel day, and this agent does not answer it by writing a
number into a booking request.

WHERE THE FACTS COME FROM
=========================
`TravelRequest` is a Pydantic v2 model supplied by the caller (§11: "no raw dicts
across a layer boundary"). The agent does not invent the traveller, the cities or
the dates, and it has no port that could fetch them: `app.agents.ports` carries no
deployment or trainer read, and inventing one here would be a schema decision this
phase does not own. This is the same division `PAYOUT_TOOLS` documents in
`app.agents.tools.catalog` — the caller that read the record passes the facts in;
the agent explains them.

The one fact the agent does read for itself is the program (`read_program`), so
the draft is titled and dated against the system of record rather than against
whatever the caller said the college was called.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.agents.ports import ContactSnapshot, Draft, ProgramSnapshot
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
    "IncompleteItineraryError",
    "Itinerary",
    "LegDirection",
    "LogisticsAgent",
    "LogisticsResult",
    "TravelAssessment",
    "TravelLeg",
    "TravelRequest",
    "detect_travel_need",
    "plan_itinerary",
]

_log = structlog.get_logger(__name__)

_BOOKING_SYSTEM: Final[str] = (
    "You draft internal travel booking REQUESTS for byteXL operations. A colleague on the "
    "travel desk reads the request and does the booking.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the cities, dates and names in the structured data given to you. State no "
    "number that is not there.\n"
    "2. Every request is a ROUND TRIP. Write the onward leg and the return leg, both, in "
    "that order. Never write a one-way request.\n"
    "3. State no fare, no per-diem, no allowance, no total and no currency. Cost is not "
    "yours to state.\n"
    "4. Do not choose or adjust a date. The dates are given; quote them.\n"
    "5. Six sentences at most. Colleagues, not customers: direct, no marketing tone.\n"
    "6. A human will read, edit and book from this. Do not write as if anything has been "
    "booked or confirmed."
)


class LegDirection(StrEnum):
    """Which way a leg goes.

    Two members, and no third. There is no `ONE_WAY`: §8 says "onward + return",
    and a direction vocabulary that could express a single-leg trip would make
    the round trip a convention again instead of a type.

    Lives here rather than in `app/domain/enums.py` for the reason
    `app.agents.tools.catalog.ToolEffect` documents at length — a vocabulary
    owned by one module, not stored in a column, belongs beside its only
    consumer. Move it to `domain/` verbatim if a column ever stores it.
    """

    ONWARD = "onward"
    RETURN = "return"


class IncompleteItineraryError(RuntimeError):
    """An itinerary was not a round trip.

    A `RuntimeError` rather than a `ValueError`, and the choice is deliberate:
    this module parses dates and cities, and a broad `except ValueError` around
    that parsing must never be able to swallow "the return leg is missing or
    wrong". That is the one failure §8 emphasises, and it must be loud.
    """


class TravelRequest(BaseModel):
    """What the caller knows about one trainer's deployment travel.

    Pydantic v2 at the layer boundary (§11), frozen and `extra="forbid"` so an
    unexpected field is a loud error rather than a silently ignored one — a
    caller that thought it was passing `return_on` and had it dropped would get
    exactly the failure this module exists to prevent.

    `traveller_pan` is the identity. §6: "Trainer identity is PAN. It is the only
    stable key present in every legacy sheet. Never match trainers by name
    string." `traveller_name` is carried alongside because a travel desk books a
    ticket in a name, not in a PAN — but the PAN is what the record is keyed on.

    **The PAN never reaches the model.** It is PII (§4), a travel desk has no use
    for it, and its four digits would enter the grounded value set and silently
    license the model to write those digits in any sentence it liked. It goes
    into the draft's `payload`, under the artifact's own access rules, and stops
    there.

    PAN *format* is not validated here. §7 owns that gate — `PAN_INVALID`, in
    `app/services/remuneration/validators.py` — and a second regex in a second
    module is a second thing to get out of step. This model asks only that the
    field is present, which is what it needs to key a record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    traveller_name: str = Field(min_length=1)
    traveller_pan: str = Field(min_length=1)
    #: Where the trainer travels from — their base, not the college.
    origin_city: str = Field(min_length=1)
    destination_city: str = Field(min_length=1)
    #: The day the trainer is due on campus, and the day they are released. Both
    #: come from the deployment record; neither is chosen here.
    report_on: dt.date
    release_on: dt.date

    @model_validator(mode="after")
    def _period_runs_forwards(self) -> TravelRequest:
        if self.release_on < self.report_on:
            raise ValueError(
                f"release_on ({self.release_on.isoformat()}) precedes report_on "
                f"({self.report_on.isoformat()}); a deployment cannot end before it starts"
            )
        return self


@dataclass(frozen=True, slots=True)
class TravelLeg:
    """One journey. Never constructed alone — see `Itinerary`."""

    direction: LegDirection
    travel_on: dt.date
    from_city: str
    to_city: str

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "direction": self.direction.value,
            "travel_on": self.travel_on.isoformat(),
            "from_city": self.from_city,
            "to_city": self.to_city,
        }


@dataclass(frozen=True, slots=True)
class Itinerary:
    """A round trip. Both legs are required fields — property 1 of the four.

    `return_leg` rather than `return` because the latter is a keyword; the name is
    ugly and it is the right trade, because `legs` below is what most callers
    touch and it always has length two.
    """

    onward: TravelLeg
    return_leg: TravelLeg

    def __post_init__(self) -> None:
        """Property 3: refuse anything that is not a genuine round trip.

        The `isinstance` guards look redundant against the annotations, and
        against a type checker they are. They are not redundant against a caller
        that built this from deserialised JSON or from `dataclasses.replace` with
        a `None`, which is exactly how a return leg goes missing in code that was
        typed correctly when it was written.
        """
        for role, leg, expected in (
            ("onward", self.onward, LegDirection.ONWARD),
            ("return", self.return_leg, LegDirection.RETURN),
        ):
            if not isinstance(leg, TravelLeg):
                raise IncompleteItineraryError(
                    f"Itinerary is missing its {role} leg. CLAUDE.md §8 gives this agent "
                    '"onward + return"; a one-way itinerary strands a trainer and is not '
                    "representable here."
                )
            if leg.direction is not expected:
                raise IncompleteItineraryError(
                    f"Itinerary's {role} leg carries direction '{leg.direction.value}', "
                    f"expected '{expected.value}'."
                )
        if self.return_leg.travel_on < self.onward.travel_on:
            raise IncompleteItineraryError(
                f"return leg travels on {self.return_leg.travel_on.isoformat()}, before the "
                f"onward leg on {self.onward.travel_on.isoformat()}"
            )
        if (
            self.return_leg.from_city != self.onward.to_city
            or self.return_leg.to_city != self.onward.from_city
        ):
            raise IncompleteItineraryError(
                f"the return leg ({self.return_leg.from_city} -> {self.return_leg.to_city}) "
                f"does not reverse the onward leg ({self.onward.from_city} -> "
                f"{self.onward.to_city}); a leg that goes somewhere else is a second trip, "
                "not a return"
            )

    @property
    def legs(self) -> tuple[TravelLeg, TravelLeg]:
        """Both legs, onward first. Always length two, by construction."""
        return (self.onward, self.return_leg)

    def as_payload(self) -> dict[str, JsonValue]:
        """The structure the travel desk books from. Two legs, every time."""
        return {"legs": [leg.as_payload() for leg in self.legs]}


@dataclass(frozen=True, slots=True)
class TravelAssessment:
    """Whether travel is needed, why, and — when it is — the whole round trip.

    The invariant `required == (itinerary is not None)` is checked rather than
    documented, so "travel is required and here is no itinerary" is not a state a
    caller can be handed. That state is the bug this agent exists to prevent,
    wearing a different hat.
    """

    required: bool
    reason: str
    itinerary: Itinerary | None = None

    def __post_init__(self) -> None:
        if self.required and self.itinerary is None:
            raise IncompleteItineraryError(
                "travel was assessed as required but no itinerary was planned — CLAUDE.md "
                '§8 gives this agent "onward + return", and a required trip with no legs '
                "is the failure that strands a trainer"
            )
        if not self.required and self.itinerary is not None:
            raise IncompleteItineraryError(
                "an itinerary was planned for a trip assessed as not required; one of the "
                "two is wrong and guessing which would book somebody a ticket they do not need"
            )

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "travel_required": self.required,
            "reason": self.reason,
            "itinerary": self.itinerary.as_payload() if self.itinerary else None,
        }


def _normalise_city(city: str) -> str:
    """Case- and whitespace-insensitive, and nothing more.

    Deliberately not fuzzy. "Hyd" does not match "Hyderabad" here, and it must
    not: a fuzzy match that decides two names are the same city *suppresses* a
    booking, and a suppressed booking strands a trainer. A fuzzy match that is
    too strict merely produces a draft a human reads and discards. When in doubt
    this module drafts, because the two errors do not cost the same.
    """
    return " ".join(city.split()).casefold()


def plan_itinerary(request: TravelRequest) -> Itinerary:
    """Both legs, derived together. Property 2 of the four — the only constructor.

    The onward leg travels on the reporting date and the return leg on the release
    date. **No buffer day is added.** Travelling the evening before a 9am start is
    obviously sensible and is equally obviously a policy nobody has written down;
    CLAUDE.md §14 carries the open questions rather than answering them, and a
    silent -1 day here would put an invented fact into a booking request. The
    travel desk moves the date if the policy says so, and a human sees it move.
    """
    return Itinerary(
        onward=TravelLeg(
            direction=LegDirection.ONWARD,
            travel_on=request.report_on,
            from_city=request.origin_city,
            to_city=request.destination_city,
        ),
        return_leg=TravelLeg(
            direction=LegDirection.RETURN,
            travel_on=request.release_on,
            from_city=request.destination_city,
            to_city=request.origin_city,
        ),
    )


def detect_travel_need(request: TravelRequest) -> TravelAssessment:
    """§8's "travel need detection". Pure, deterministic, no model involved.

    One rule: travel is needed when the trainer's base and the campus are not the
    same city. That is the whole of it, and it is a comparison rather than a
    judgement — R1's line, applied to something that is not money. A model asked
    "does this deployment need travel?" would answer differently on Tuesday.

    A same-city deployment returns `required=False` and **no itinerary**, and the
    agent then drafts nothing and calls no model. A booking request for a journey
    across one city is noise on a travel desk, and noise is how a desk learns to
    skim.
    """
    if _normalise_city(request.origin_city) == _normalise_city(request.destination_city):
        return TravelAssessment(
            required=False,
            reason=(
                f"origin and destination are the same city ({request.destination_city}); "
                "no travel booking is needed"
            ),
        )
    return TravelAssessment(
        required=True,
        reason=(
            f"trainer travels from {request.origin_city} to {request.destination_city} "
            "for this deployment"
        ),
        itinerary=plan_itinerary(request),
    )


def _internal_only(contacts: Sequence[ContactSnapshot]) -> tuple[ContactSnapshot, ...]:
    """Drop external parties. §4's internal personas; §8's Draft ceiling.

    A booking request goes to a byteXL travel desk — Central OPS in §4's list of
    internal actors. Addressing one at a college contact would be an external
    communication drafted by an agent whose ceiling does not contemplate one, and
    R3 gives nothing in this layer the capability to deliver it anyway.
    """
    return tuple(contact for contact in contacts if contact.persona in INTERNAL_PERSONAS)


@dataclass(frozen=True, slots=True)
class LogisticsResult:
    """The assessment and, when travel was needed, the persisted draft.

    Both are returned for the reason `app.agents.intake.IntakeResult` gives: the
    caller usually wants the structure for a form and the outcome for the audit
    trail, and fetching one back from the other would mean a read.

    `outcome` is `None` exactly when no travel was needed, and `__post_init__`
    holds that to be true in both directions — a draft saved for a trip nobody
    needs is as wrong as a needed trip with no draft.
    """

    assessment: TravelAssessment
    outcome: DraftOutcome | None = None

    def __post_init__(self) -> None:
        if self.assessment.required != (self.outcome is not None):
            raise IncompleteItineraryError(
                f"travel_required={self.assessment.required} but "
                f"outcome={'a draft' if self.outcome else 'None'}; a booking request is "
                "drafted for exactly those deployments that need one"
            )

    @property
    def itinerary(self) -> Itinerary | None:
        return self.assessment.itinerary


@dataclass
class LogisticsAgent:
    """Detects travel need and drafts the round-trip booking request. Level 2 (§8)."""

    runtime: AgentRuntime

    def __post_init__(self) -> None:
        if self.runtime.agent is not AgentName.LOGISTICS:
            raise ValueError(
                f"LogisticsAgent needs the logistics runtime, got '{self.runtime.agent.value}'"
            )
        if self.runtime.autonomy > AutonomyLevel.DRAFT:
            raise ValueError(
                "Logistics' ceiling is Draft (CLAUDE.md §8). It drafts a booking request; "
                "a human on the travel desk books the travel."
            )

    async def draft_booking_request(
        self,
        program_id: UUID,
        request: TravelRequest,
        *,
        actor_id: UUID | None = None,
        actor_persona: Persona | None = None,
    ) -> LogisticsResult:
        """Assess the need and, if there is one, draft the round-trip request.

        Order matters: the need is detected and the itinerary planned *before*
        the model is called, so the prose describes a trip that was derived. When
        no travel is needed the method returns early — no model call, no draft, no
        tokens spent telling a travel desk there is nothing to book.

        Both legs are in `payload["itinerary"]["legs"]` on every draft this
        method saves. That is property 4 of the four in the module docstring: the
        desk books from the structure, so a model that under-writes the covering
        note cannot produce a one-way trip.
        """
        assessment = detect_travel_need(request)
        if not assessment.required:
            _log.info(
                "logistics.no_travel_needed",
                program_id=str(program_id),
                destination=request.destination_city,
            )
            return LogisticsResult(assessment=assessment)

        itinerary = assessment.itinerary
        if itinerary is None:  # pragma: no cover - TravelAssessment forbids this
            raise IncompleteItineraryError(
                "travel is required but the assessment carries no itinerary"
            )

        program = await self.runtime.dispatcher.read_program(program_id)
        tasks = await self.runtime.dispatcher.list_program_tasks(program_id)
        contacts = _internal_only(await self.runtime.dispatcher.list_internal_contacts(program_id))

        grounded_in: dict[str, JsonValue] = {
            "program": _program_payload(program),
            # Name only. The PAN is identity, not prompt material — see
            # `TravelRequest`.
            "traveller_name": request.traveller_name,
            "reason": assessment.reason,
            "itinerary": itinerary.as_payload(),
            "open_deployment_tasks": [
                {
                    "title": task.title,
                    "status": task.status.value,
                    "due_on": task.due_on.isoformat() if task.due_on else None,
                }
                for task in tasks
                if task.stage is ProgramStage.DEPLOYMENT
            ],
            "recipients": [contact.display_name for contact in contacts],
        }
        body, invocation = await self.runtime.generate(
            LLMTask.DRAFTING,
            system=_BOOKING_SYSTEM,
            user=(
                "ROUND TRIP TO BE BOOKED (the only cities, dates and names you may state; "
                "both legs must appear):\n"
                f"{json.dumps(grounded_in, indent=2, default=str)}"
            ),
            structured_input=grounded_in,
            context="logistics.booking_request",
        )
        draft = Draft(
            artifact_type=ArtifactType.PROGRAM_DOCUMENT,
            title=f"Travel booking request — {request.traveller_name}, {_college_of(program)}",
            body=body,
            payload={
                "traveller": {
                    "name": request.traveller_name,
                    # §6: PAN is the stable key. It rides in the payload, which
                    # lives under the artifact's own access rules, and never in
                    # the prompt.
                    "pan": request.traveller_pan,
                },
                "itinerary": itinerary.as_payload(),
                "recipients": [contact.display_name for contact in contacts],
            },
            program_id=program_id,
            flags=_booking_flags(itinerary, contacts),
            grounded_in=grounded_in,
        )
        _log.info(
            "logistics.booking_drafted",
            program_id=str(program_id),
            legs=len(itinerary.legs),
            onward_on=itinerary.onward.travel_on.isoformat(),
            return_on=itinerary.return_leg.travel_on.isoformat(),
            recipients=len(contacts),
        )
        outcome = await self.runtime.save_draft(
            draft, invocation, actor_id=actor_id, actor_persona=actor_persona
        )
        return LogisticsResult(assessment=assessment, outcome=outcome)


def _booking_flags(itinerary: Itinerary, contacts: Sequence[ContactSnapshot]) -> tuple[str, ...]:
    """Non-blocking observations for the reviewer. Never auto-resolved."""
    flags: list[str] = []
    if not contacts:
        flags.append("no internal recipient identified — assign before sending")
    if itinerary.return_leg.travel_on == itinerary.onward.travel_on:
        flags.append("onward and return fall on the same day — confirm this is a day trip")
    return tuple(flags)


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
