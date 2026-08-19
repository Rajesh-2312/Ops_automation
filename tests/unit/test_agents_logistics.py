"""The Logistics agent. CLAUDE.md §8: "Travel need detection, booking request,
onward + **return**". Ceiling: Draft.

Most of this file is about one word. §8 emphasises "return" and nothing else in
that whole table, because a trainer finishing a deployment eight hundred
kilometres from home with no ticket back is the failure the agent exists to
prevent. The module makes the round trip structural rather than instructed, and
the assertions below attack it from each of the angles a regression would arrive
from: the type has no one-way shape, the only constructor emits both legs, a
return leg that does not reverse the onward one is refused, and every draft the
agent saves carries two legs in its payload whatever the model wrote.

The rest is the usual R1/R2/R3 perimeter — the model states no date it was not
given, no amount at all, and nothing is booked.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.grounding import UngroundedFigureError, collect_grounded_values, figures_in
from app.agents.logistics import (
    IncompleteItineraryError,
    Itinerary,
    LegDirection,
    LogisticsAgent,
    LogisticsResult,
    TravelAssessment,
    TravelLeg,
    TravelRequest,
    detect_travel_need,
    plan_itinerary,
)
from app.agents.runtime import AgentRuntime, AutonomyCeilingError
from app.agents.tools import AgentName, PortBundle, ToolEffect, bind, toolset_for
from app.domain.enums import ArtifactState, AutonomyLevel, LLMTask, Persona, ProgramStage
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeDraftSink,
    FakeLLM,
    FakeProgramPort,
    a_contact,
    a_program,
    a_task,
)

REPORT_ON = dt.date(2026, 7, 26)
RELEASE_ON = dt.date(2026, 7, 31)


def a_request(
    *,
    origin: str = "Hyderabad",
    destination: str = "Guntur",
    report_on: dt.date = REPORT_ON,
    release_on: dt.date = RELEASE_ON,
) -> TravelRequest:
    return TravelRequest(
        traveller_name="VEMA PRUDHVI SAI",
        traveller_pan="ABCDE1234F",
        origin_city=origin,
        destination_city=destination,
        report_on=report_on,
        release_on=release_on,
    )


# --- the round trip is structural (§8's bolded word) -------------------------


def test_an_itinerary_cannot_be_built_without_a_return_leg() -> None:
    """Property 1: both legs are required fields. A one-way trip has no shape here."""
    onward = TravelLeg(LegDirection.ONWARD, REPORT_ON, "Hyderabad", "Guntur")
    with pytest.raises(TypeError):
        Itinerary(onward=onward)  # type: ignore[call-arg]


def test_a_none_return_leg_is_refused_loudly() -> None:
    """The way a return leg actually goes missing: dynamic construction from
    deserialised data, in code that was typed correctly when it was written."""
    onward = TravelLeg(LegDirection.ONWARD, REPORT_ON, "Hyderabad", "Guntur")
    with pytest.raises(IncompleteItineraryError, match="missing its return leg"):
        Itinerary(onward=onward, return_leg=None)  # type: ignore[arg-type]


def test_the_only_constructor_always_emits_both_legs() -> None:
    """Property 2: there is no `plan_onward()` to call and forget to follow up."""
    itinerary = plan_itinerary(a_request())
    assert len(itinerary.legs) == 2
    assert [leg.direction for leg in itinerary.legs] == [LegDirection.ONWARD, LegDirection.RETURN]


def test_the_return_leg_reverses_the_onward_leg() -> None:
    itinerary = plan_itinerary(a_request(origin="Hyderabad", destination="Guntur"))
    assert (itinerary.onward.from_city, itinerary.onward.to_city) == ("Hyderabad", "Guntur")
    assert (itinerary.return_leg.from_city, itinerary.return_leg.to_city) == (
        "Guntur",
        "Hyderabad",
    )


def test_the_legs_carry_the_deployment_dates_and_no_invented_buffer() -> None:
    """§14 carries the open questions; a silent -1 day would answer one by writing
    an invented fact into a booking request."""
    itinerary = plan_itinerary(a_request())
    assert itinerary.onward.travel_on == REPORT_ON
    assert itinerary.return_leg.travel_on == RELEASE_ON


def test_a_return_leg_going_somewhere_else_is_not_a_return() -> None:
    """Property 3. A leg to a third city is a second trip, and booking it as a
    return is how somebody ends up in the wrong state."""
    with pytest.raises(IncompleteItineraryError, match="does not reverse"):
        Itinerary(
            onward=TravelLeg(LegDirection.ONWARD, REPORT_ON, "Hyderabad", "Guntur"),
            return_leg=TravelLeg(LegDirection.RETURN, RELEASE_ON, "Guntur", "Chennai"),
        )


def test_a_return_leg_before_the_onward_leg_is_refused() -> None:
    with pytest.raises(IncompleteItineraryError, match="before the"):
        Itinerary(
            onward=TravelLeg(LegDirection.ONWARD, RELEASE_ON, "Hyderabad", "Guntur"),
            return_leg=TravelLeg(LegDirection.RETURN, REPORT_ON, "Guntur", "Hyderabad"),
        )


def test_two_onward_legs_are_not_an_itinerary() -> None:
    """The direction is checked, not assumed from the field it was passed in."""
    with pytest.raises(IncompleteItineraryError, match="carries direction 'onward'"):
        Itinerary(
            onward=TravelLeg(LegDirection.ONWARD, REPORT_ON, "Hyderabad", "Guntur"),
            return_leg=TravelLeg(LegDirection.ONWARD, RELEASE_ON, "Guntur", "Hyderabad"),
        )


def test_there_is_no_one_way_direction_to_express() -> None:
    """A vocabulary that could name a single-leg trip would make the round trip a
    convention again instead of a type."""
    assert {direction.value for direction in LegDirection} == {"onward", "return"}


def test_a_day_trip_is_a_round_trip_not_a_one_way() -> None:
    """Same-day return is legal and still two legs. It is flagged, not dropped."""
    itinerary = plan_itinerary(a_request(report_on=REPORT_ON, release_on=REPORT_ON))
    assert len(itinerary.legs) == 2
    assert itinerary.return_leg.travel_on == itinerary.onward.travel_on


# --- travel need detection: pure, deterministic (R1) -------------------------


def test_a_different_city_needs_travel_and_gets_a_full_itinerary() -> None:
    assessment = detect_travel_need(a_request(origin="Hyderabad", destination="Guntur"))
    assert assessment.required
    assert assessment.itinerary is not None
    assert len(assessment.itinerary.legs) == 2


def test_the_same_city_needs_no_travel_and_gets_no_itinerary() -> None:
    assessment = detect_travel_need(a_request(origin="Guntur", destination="Guntur"))
    assert not assessment.required
    assert assessment.itinerary is None


def test_city_comparison_ignores_case_and_spacing_only() -> None:
    assessment = detect_travel_need(a_request(origin="  guntur ", destination="Guntur"))
    assert not assessment.required


def test_comparison_is_not_fuzzy_and_errs_towards_drafting() -> None:
    """A fuzzy match that decided "Hyd" and "Hyderabad" were one city would
    *suppress* a booking. A spurious draft is read and discarded; a suppressed
    booking strands a trainer. The two errors do not cost the same."""
    assessment = detect_travel_need(a_request(origin="Hyd", destination="Hyderabad"))
    assert assessment.required


def test_detection_is_deterministic() -> None:
    request = a_request()
    assert detect_travel_need(request).as_payload() == detect_travel_need(request).as_payload()


def test_a_required_trip_with_no_itinerary_is_unrepresentable() -> None:
    """The invariant is checked, not documented — it is the same bug wearing a
    different hat."""
    with pytest.raises(IncompleteItineraryError, match="no itinerary was planned"):
        TravelAssessment(required=True, reason="trainer travels")


def test_an_unneeded_trip_with_an_itinerary_is_unrepresentable() -> None:
    with pytest.raises(IncompleteItineraryError, match="not required"):
        TravelAssessment(required=False, reason="same city", itinerary=plan_itinerary(a_request()))


# --- the request model: Pydantic v2 at the boundary (§11) --------------------


def test_a_deployment_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValidationError, match="precedes report_on"):
        a_request(report_on=RELEASE_ON, release_on=REPORT_ON)


def test_an_unexpected_field_is_a_loud_error_not_a_silent_drop() -> None:
    """A caller that thought it was passing `return_on` and had it ignored would
    get exactly the failure this module exists to prevent."""
    with pytest.raises(ValidationError):
        TravelRequest(
            traveller_name="X",
            traveller_pan="ABCDE1234F",
            origin_city="Hyderabad",
            destination_city="Guntur",
            report_on=REPORT_ON,
            release_on=RELEASE_ON,
            return_on=RELEASE_ON,  # type: ignore[call-arg]
        )


def test_the_request_is_frozen() -> None:
    request = a_request()
    with pytest.raises(ValidationError):
        request.destination_city = "Chennai"


def test_an_empty_traveller_is_refused() -> None:
    with pytest.raises(ValidationError):
        TravelRequest(
            traveller_name="",
            traveller_pan="ABCDE1234F",
            origin_city="Hyderabad",
            destination_city="Guntur",
            report_on=REPORT_ON,
            release_on=RELEASE_ON,
        )


# --- the agent ---------------------------------------------------------------


def build_agent(
    llm: FakeLLM, *, contacts: tuple = (), tasks: tuple = ()
) -> tuple[LogisticsAgent, FakeDraftSink]:
    sink = FakeDraftSink()
    ports = PortBundle(
        programs=FakeProgramPort(
            program=a_program(ProgramStage.DEPLOYMENT), contacts=contacts, tasks=tasks
        ),
        drafts=sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.LOGISTICS,
        dispatcher=bind(toolset_for(AgentName.LOGISTICS), ports),
        llm=llm,
    )
    return LogisticsAgent(runtime=runtime), sink


async def test_a_booking_request_is_drafted_never_booked() -> None:
    """R3/R4: "booking request" in §8 is a noun. It lands in DRAFT."""
    agent, sink = build_agent(
        FakeLLM(responses=["Please book Hyderabad to Guntur on 2026-07-26, back on 2026-07-31."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    result = await agent.draft_booking_request(PROGRAM_ID, a_request())
    assert result.outcome is not None
    assert result.outcome.saved.state is ArtifactState.DRAFT
    assert len(sink.saved) == 1


async def test_every_saved_draft_carries_both_legs_in_its_payload() -> None:
    """Property 4: the desk books from the structure, so a model that under-writes
    the covering note cannot produce a one-way trip."""
    agent, sink = build_agent(
        FakeLLM(responses=["Please book travel for this deployment."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    await agent.draft_booking_request(PROGRAM_ID, a_request())
    draft, _ = sink.saved[0]
    legs = draft.payload["itinerary"]["legs"]  # type: ignore[index,call-overload]
    assert [leg["direction"] for leg in legs] == ["onward", "return"]
    assert [leg["travel_on"] for leg in legs] == ["2026-07-26", "2026-07-31"]


async def test_no_travel_needed_means_no_model_call_and_no_draft() -> None:
    """A booking request for a journey across one city is noise on a travel desk,
    and noise is how a desk learns to skim."""
    llm = FakeLLM(responses=[])
    agent, sink = build_agent(llm, contacts=(a_contact(Persona.MANAGER),))
    result = await agent.draft_booking_request(
        PROGRAM_ID, a_request(origin="Guntur", destination="Guntur")
    )
    assert not result.assessment.required
    assert result.outcome is None
    assert llm.calls == []
    assert sink.saved == []


async def test_the_booking_request_routes_to_the_volume_tier() -> None:
    """§2: drafting is volume work, routed by task and not by default."""
    llm = FakeLLM(responses=["Please book the round trip."])
    agent, _ = build_agent(llm, contacts=(a_contact(Persona.MANAGER),))
    result = await agent.draft_booking_request(PROGRAM_ID, a_request())
    assert llm.tasks_called == [LLMTask.DRAFTING]
    assert result.outcome is not None
    assert result.outcome.invocation.model == "fake/volume"


async def test_the_request_is_addressed_only_to_internal_colleagues() -> None:
    """§4/§8: an external party is filtered out before the model is called."""
    llm = FakeLLM(responses=["Please book the round trip."])
    agent, sink = build_agent(
        llm,
        contacts=(
            a_contact(Persona.MANAGER, "R. Maroju"),
            a_contact(Persona.TRAINER, "VEMA PRUDHVI SAI"),
            a_contact(Persona.COLLEGE, "Malineni Principal"),
        ),
    )
    await agent.draft_booking_request(PROGRAM_ID, a_request())
    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == ["R. Maroju"]
    assert "Malineni Principal" not in str(llm.calls[0]["user"])


async def test_a_request_with_no_internal_recipient_is_flagged() -> None:
    agent, sink = build_agent(
        FakeLLM(responses=["Please book the round trip."]),
        contacts=(a_contact(Persona.COLLEGE, "Malineni Principal"),),
    )
    await agent.draft_booking_request(PROGRAM_ID, a_request())
    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == []
    assert "no internal recipient identified — assign before sending" in draft.flags


async def test_a_same_day_round_trip_is_flagged_for_confirmation() -> None:
    agent, sink = build_agent(
        FakeLLM(responses=["Please book the round trip."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    await agent.draft_booking_request(
        PROGRAM_ID, a_request(report_on=REPORT_ON, release_on=REPORT_ON)
    )
    draft, _ = sink.saved[0]
    assert "onward and return fall on the same day — confirm this is a day trip" in draft.flags


async def test_open_deployment_tasks_reach_the_prompt_as_context() -> None:
    llm = FakeLLM(responses=["Please book the round trip."])
    agent, _ = build_agent(
        llm,
        contacts=(a_contact(Persona.MANAGER),),
        tasks=(
            a_task("Confirm campus accommodation", stage=ProgramStage.DEPLOYMENT),
            a_task("Collect feedback", stage=ProgramStage.CLOSEOUT_FINANCE),
        ),
    )
    await agent.draft_booking_request(PROGRAM_ID, a_request())
    prompt = str(llm.calls[0]["user"])
    assert "Confirm campus accommodation" in prompt
    assert "Collect feedback" not in prompt


# --- R1 / §12: no figure the structured input did not contain ----------------


async def test_an_invented_date_is_refused() -> None:
    """§12: "compare every number in generated text against the structured input".
    A travel date the itinerary does not contain is a fabrication, and the draft is
    refused rather than corrected."""
    agent, sink = build_agent(
        FakeLLM(responses=["Please book the return for 2026-08-04."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_booking_request(PROGRAM_ID, a_request())
    assert exc.value.context == "logistics.booking_request"
    assert not sink.saved


async def test_an_invented_fare_is_refused() -> None:
    """R2: this agent states no amount at all. Travel money is a remuneration
    input computed in `services/remuneration/engine.py`, in Decimal."""
    agent, sink = build_agent(
        FakeLLM(responses=["Book the round trip; the fare is approximately 4,500."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    with pytest.raises(UngroundedFigureError):
        await agent.draft_booking_request(PROGRAM_ID, a_request())
    assert not sink.saved


async def test_every_figure_in_the_drafted_body_is_in_the_structured_input() -> None:
    """§12's assertion made directly, rather than trusting the runtime's check."""
    agent, sink = build_agent(
        FakeLLM(
            responses=[
                "Onward 2026-07-26 Hyderabad to Guntur; return 2026-07-31 Guntur to " "Hyderabad."
            ]
        ),
        contacts=(a_contact(Persona.MANAGER),),
    )
    await agent.draft_booking_request(PROGRAM_ID, a_request())
    draft, _ = sink.saved[0]
    allowed = collect_grounded_values(draft.grounded_in)
    assert figures_in(draft.body), "the assertion is worthless if the body has no figures"
    for written, value in figures_in(draft.body):
        assert value in allowed, f"{written!r} is not in the structured input"


async def test_the_pan_is_kept_out_of_the_prompt_and_kept_in_the_payload() -> None:
    """§6 makes PAN the trainer's identity, so it keys the payload. §4 makes it
    PII, so it does not go to the model — and its four digits would otherwise
    enter the grounded value set and license the model to write them anywhere."""
    llm = FakeLLM(responses=["Please book the round trip."])
    agent, sink = build_agent(llm, contacts=(a_contact(Persona.MANAGER),))
    await agent.draft_booking_request(PROGRAM_ID, a_request())

    draft, _ = sink.saved[0]
    assert draft.payload["traveller"]["pan"] == "ABCDE1234F"  # type: ignore[index,call-overload]
    assert "ABCDE1234F" not in str(llm.calls[0]["user"])
    assert "ABCDE1234F" not in str(draft.grounded_in)
    assert 1234 not in collect_grounded_values(draft.grounded_in)


# --- R3 and the ceiling ------------------------------------------------------


def test_the_logistics_toolset_holds_no_release_capable_tool() -> None:
    """§12: "assert no agent toolset exposes a release-capable tool". Asserted on
    the effects, not on the names — the strong form."""
    toolset = toolset_for(AgentName.LOGISTICS)
    assert toolset.effects <= {ToolEffect.READ, ToolEffect.SAVE_DRAFT}
    forbidden_names = (
        "send_email",
        "send_whatsapp",
        "post_message",
        "mark_released",
        "book_travel",
    )
    for forbidden in forbidden_names:
        assert forbidden not in toolset.names


def test_logistics_cannot_be_built_above_the_draft_ceiling() -> None:
    with pytest.raises(AutonomyCeilingError):
        AgentRuntime(
            agent=AgentName.LOGISTICS,
            dispatcher=bind(toolset_for(AgentName.LOGISTICS), PortBundle()),
            llm=FakeLLM(),
            autonomy=AutonomyLevel.ACT,
        )


def test_the_agent_refuses_another_agents_runtime() -> None:
    runtime = AgentRuntime(
        agent=AgentName.SOURCING,
        dispatcher=bind(toolset_for(AgentName.SOURCING), PortBundle()),
        llm=FakeLLM(),
    )
    with pytest.raises(ValueError, match="logistics runtime"):
        LogisticsAgent(runtime=runtime)


async def test_everything_the_agent_did_before_generating_was_a_read() -> None:
    """§11's "tools called" record, read back as an R3 assertion."""
    agent, sink = build_agent(
        FakeLLM(responses=["Please book the round trip."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    result = await agent.draft_booking_request(PROGRAM_ID, a_request())
    assert result.outcome is not None
    assert [call.tool for call in result.outcome.invocation.tools_called] == [
        "read_program",
        "list_program_tasks",
        "list_internal_contacts",
    ]
    assert all(call.effect is ToolEffect.READ for call in result.outcome.invocation.tools_called)
    assert len(sink.saved) == 1


async def test_the_audit_row_records_the_agent_and_the_model() -> None:
    """§11: every state transition writes an AuditEvent — actor, action, before, after."""
    agent, sink = build_agent(
        FakeLLM(responses=["Please book the round trip."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    actor = uuid4()
    await agent.draft_booking_request(
        PROGRAM_ID, a_request(), actor_id=actor, actor_persona=Persona.MANAGER
    )
    _, event = sink.saved[0]
    assert event.actor_id == actor
    assert event.action == "agent.draft_saved"
    assert event.before is None
    after = event.after or {}
    assert after["agent"] == "logistics"
    assert after["autonomy"] == AutonomyLevel.DRAFT.value


def test_a_result_cannot_claim_travel_is_needed_with_no_draft() -> None:
    """A booking request is drafted for exactly those deployments that need one."""
    with pytest.raises(IncompleteItineraryError, match="travel_required=True"):
        LogisticsResult(assessment=detect_travel_need(a_request()))
