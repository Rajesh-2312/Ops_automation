"""The Sourcing Liaison. CLAUDE.md §8: "Requirement spec, TA follow-up, re-spec
diffs, profile ranking". Ceiling: Draft.

The assertions divide the way the module does. Ranking and diffing are pure
functions and are tested as arithmetic — deterministically, with no model in
sight. The prose paths are tested with a mocked LLM, and what they assert is that
the model may *explain* the ranking and may not restate, re-rank or recompute it
(R1, and R2's "an agent may explain a number, it may never produce one").
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents.grounding import UngroundedFigureError
from app.agents.ports import TrainerProfileSnapshot
from app.agents.runtime import AgentRuntime, AutonomyCeilingError
from app.agents.sourcing import SourcingAgent, diff_spec, rank_profiles
from app.agents.tools import AgentName, PortBundle, bind, toolset_for
from app.domain.enums import ArtifactState, AutonomyLevel, LLMTask, Persona, ProgramStage
from tests.unit.agent_fakes import (
    PROGRAM_ID,
    FakeDraftSink,
    FakeLLM,
    FakeProgramPort,
    FakeRetrievalPort,
    FakeSourcingPort,
    a_contact,
    a_program,
    a_task,
)


def a_profile(
    name: str, skills: tuple[str, ...] = (), years: str | None = None
) -> TrainerProfileSnapshot:
    return TrainerProfileSnapshot(
        profile_id=PROGRAM_ID,
        display_name=name,
        skills=skills,
        years_experience=Decimal(years) if years is not None else None,
    )


# --- ranking: pure Python, no LLM (R1/R2) ------------------------------------


def test_full_skill_coverage_outranks_partial() -> None:
    ranked = rank_profiles(
        [a_profile("Partial", ("python",), "4"), a_profile("Full", ("python", "sql"), "4")],
        ["python", "sql"],
    )
    assert [r.profile.display_name for r in ranked] == ["Full", "Partial"]


def test_skill_matching_is_case_insensitive_but_exact() -> None:
    """ "Java" must not match "JavaScript" — the costly false positive here."""
    ranked = rank_profiles([a_profile("A", ("JavaScript",), "5")], ["java"])
    assert ranked[0].matched_skills == ()
    assert ranked[0].missing_skills == ("java",)


def test_missing_skills_are_named() -> None:
    ranked = rank_profiles([a_profile("A", ("python",))], ["python", "sql", "aws"])
    assert ranked[0].matched_skills == ("python",)
    assert ranked[0].missing_skills == ("sql", "aws")


def test_ties_break_on_name_not_on_input_order() -> None:
    """A ranking that reorders because the database did costs trust for nothing."""
    forward = rank_profiles(
        [a_profile("Zoya", ("python",), "5"), a_profile("Anil", ("python",), "5")], ["python"]
    )
    reverse = rank_profiles(
        [a_profile("Anil", ("python",), "5"), a_profile("Zoya", ("python",), "5")], ["python"]
    )
    assert [r.profile.display_name for r in forward] == ["Anil", "Zoya"]
    assert [r.profile.display_name for r in forward] == [r.profile.display_name for r in reverse]


def test_scores_are_decimal_and_quantised_once() -> None:
    """R6's discipline outside money: a score that drifts on recomputation
    is as confusing as a payout that does."""
    ranked = rank_profiles([a_profile("A", ("python", "sql"), "8")], ["python", "sql"])
    assert ranked[0].score == Decimal("1.000")
    assert isinstance(ranked[0].score, Decimal)
    assert (
        rank_profiles([a_profile("A", ("python", "sql"), "8")], ["python", "sql"])[0].score
        == ranked[0].score
    )


def test_experience_is_capped() -> None:
    """Twenty years and ten years are not the discriminator that matters."""
    ranked = rank_profiles([a_profile("Ten", (), "10"), a_profile("Twenty", (), "20")], [])
    assert ranked[0].score == ranked[1].score == Decimal("1.000")


def test_a_profile_with_no_experience_still_ranks() -> None:
    ranked = rank_profiles([a_profile("New", ("python",))], ["python"])
    assert ranked[0].score == Decimal("0.750")


def test_ranking_with_no_required_skills_falls_back_to_experience() -> None:
    ranked = rank_profiles([a_profile("A", (), "4"), a_profile("B", (), "8")], [])
    assert [r.profile.display_name for r in ranked] == ["B", "A"]


def test_no_profiles_ranks_to_nothing() -> None:
    assert rank_profiles([], ["python"]) == ()


# --- the re-spec diff: a set operation ---------------------------------------


def test_a_first_spec_is_all_additions() -> None:
    diff = diff_spec(None, {"skills": ["python"], "count": 2})
    assert set(diff.added) == {"skills", "count"}
    assert not diff.removed and not diff.changed


def test_a_respec_names_both_values_of_a_change() -> None:
    """ "Skills changed" without from-what-to-what makes TA read both documents."""
    diff = diff_spec({"count": 2, "city": "Guntur"}, {"count": 4, "city": "Guntur"})
    assert diff.changed == {"count": (2, 4)}
    assert not diff.added and not diff.removed


def test_a_removed_field_is_reported() -> None:
    diff = diff_spec({"count": 2, "shift": "night"}, {"count": 2})
    assert set(diff.removed) == {"shift"}


def test_an_unchanged_respec_is_empty() -> None:
    diff = diff_spec({"count": 2}, {"count": 2})
    assert diff.is_empty


# --- the agent ---------------------------------------------------------------


def build_agent(
    llm: FakeLLM,
    *,
    profiles: tuple[TrainerProfileSnapshot, ...] = (),
    spec: dict[str, object] | None = None,
    contacts: tuple = (),
    tasks: tuple = (),
) -> tuple[SourcingAgent, FakeDraftSink]:
    sink = FakeDraftSink()
    ports = PortBundle(
        programs=FakeProgramPort(
            program=a_program(ProgramStage.TRAINER_SOURCING),
            contacts=contacts,
            tasks=tasks,
        ),
        sourcing=FakeSourcingPort(profiles=profiles, spec=spec),  # type: ignore[arg-type]
        retrieval=FakeRetrievalPort(),
        drafts=sink,
    )
    runtime = AgentRuntime(
        agent=AgentName.SOURCING,
        dispatcher=bind(toolset_for(AgentName.SOURCING), ports),
        llm=llm,
    )
    return SourcingAgent(runtime=runtime), sink


async def test_a_requirement_spec_is_drafted_not_issued() -> None:
    """R3/R4: the spec lands in DRAFT for a human to edit and send."""
    body = "We need 4 trainers in Guntur from 2026-07-01. Stipend is to be confirmed."
    agent, sink = build_agent(FakeLLM(responses=[body]))
    outcome = await agent.draft_requirement_spec(
        PROGRAM_ID, {"count": 4, "city": "Guntur", "from": "2026-07-01"}
    )
    assert outcome.saved.state is ArtifactState.DRAFT
    assert len(sink.saved) == 1


async def test_the_respec_diff_is_computed_before_the_model_sees_it() -> None:
    """The prose describes a diff that was calculated, not one that was noticed."""
    llm = FakeLLM(responses=["The head-count moves from 2 to 4; the city is unchanged."])
    agent, sink = build_agent(llm, spec={"count": 2, "city": "Guntur"})
    outcome = await agent.draft_requirement_spec(PROGRAM_ID, {"count": 4, "city": "Guntur"})

    draft, _ = sink.saved[0]
    assert draft.payload["diff"] == {"added": {}, "removed": {}, "changed": {"count": [2, 4]}}
    assert "diff" in llm.calls[0]["user"]  # type: ignore[operator]
    assert outcome.saved.state is ArtifactState.DRAFT


async def test_a_respec_with_no_changes_is_flagged() -> None:
    agent, sink = build_agent(FakeLLM(responses=["Nothing changed."]), spec={"count": 2})
    await agent.draft_requirement_spec(PROGRAM_ID, {"count": 2})
    draft, _ = sink.saved[0]
    assert draft.flags == ("re-spec with no changes",)


async def test_the_model_explains_the_ranking_and_cannot_restate_it() -> None:
    """R2's shape: scores are computed in Python; the model quotes them."""
    profiles = (
        a_profile("Anil", ("python", "sql"), "8"),
        a_profile("Zoya", ("python",), "4"),
    )
    agent, sink = build_agent(
        FakeLLM(responses=["Anil scores 1.000 with both skills. Zoya scores 0.500."]),
        profiles=profiles,
    )
    outcome = await agent.draft_shortlist(PROGRAM_ID, ["python", "sql"])
    draft, _ = sink.saved[0]
    ranking = draft.payload["ranking"]
    assert [row["name"] for row in ranking] == ["Anil", "Zoya"]  # type: ignore[index,union-attr]
    assert outcome.saved.state is ArtifactState.DRAFT


async def test_a_score_the_model_invented_is_refused() -> None:
    """The check that makes "explain, do not produce" enforceable."""
    agent, sink = build_agent(
        FakeLLM(responses=["Anil scores 0.930, comfortably the strongest candidate."]),
        profiles=(a_profile("Anil", ("python", "sql"), "8"),),
    )
    with pytest.raises(UngroundedFigureError) as exc:
        await agent.draft_shortlist(PROGRAM_ID, ["python", "sql"])
    assert exc.value.context == "sourcing.shortlist"
    assert not sink.saved


async def test_an_empty_shortlist_is_flagged_rather_than_narrated() -> None:
    agent, sink = build_agent(FakeLLM(responses=["No profiles have been submitted."]))
    await agent.draft_shortlist(PROGRAM_ID, ["python"])
    draft, _ = sink.saved[0]
    assert draft.flags == ("no profiles submitted against this requirement",)


# --- nobody external is contacted or even addressed --------------------------


async def test_a_ta_followup_is_addressed_only_to_internal_colleagues() -> None:
    """§4/§8: an external party is filtered out before the model is called."""
    contacts = (
        a_contact(Persona.MANAGER, "R. Maroju"),
        a_contact(Persona.TRAINER, "VEMA PRUDHVI SAI"),
        a_contact(Persona.COLLEGE, "Malineni Principal"),
    )
    llm = FakeLLM(responses=["Chasing the open trainer requirement; 0 profiles so far."])
    agent, sink = build_agent(
        llm, contacts=contacts, tasks=(a_task(stage=ProgramStage.TRAINER_SOURCING),)
    )
    await agent.draft_ta_followup(PROGRAM_ID)

    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == ["R. Maroju"]
    prompt = str(llm.calls[0]["user"])
    assert "VEMA PRUDHVI SAI" not in prompt
    assert "Malineni Principal" not in prompt


async def test_a_followup_with_no_internal_recipient_is_flagged_not_addressed() -> None:
    """It does not fall back to the college contact. It asks a human."""
    agent, sink = build_agent(
        FakeLLM(responses=["The requirement is still open."]),
        contacts=(a_contact(Persona.COLLEGE, "Malineni Principal"),),
    )
    await agent.draft_ta_followup(PROGRAM_ID)
    draft, _ = sink.saved[0]
    assert draft.payload["recipients"] == []
    assert draft.flags == ("no internal recipient identified — assign before sending",)


async def test_a_followup_is_a_draft_and_is_never_sent() -> None:
    """R3: this agent has no send tool, so a chase can only ever be a draft."""
    agent, sink = build_agent(
        FakeLLM(responses=["Chasing the requirement."]),
        contacts=(a_contact(Persona.MANAGER),),
    )
    outcome = await agent.draft_ta_followup(PROGRAM_ID)
    assert outcome.saved.state is ArtifactState.DRAFT
    assert not toolset_for(AgentName.SOURCING).names.__contains__("send_email")
    assert [call.tool for call in outcome.invocation.tools_called][-1:] != ["send_email"]
    assert len(sink.saved) == 1


async def test_the_chase_routes_to_the_volume_tier() -> None:
    """§2: chase is volume work, not frontier."""
    llm = FakeLLM(responses=["Chasing."])
    agent, _ = build_agent(llm, contacts=(a_contact(Persona.MANAGER),))
    await agent.draft_ta_followup(PROGRAM_ID)
    assert llm.tasks_called == [LLMTask.CHASE]


def test_sourcing_cannot_be_built_above_the_draft_ceiling() -> None:
    with pytest.raises(AutonomyCeilingError):
        AgentRuntime(
            agent=AgentName.SOURCING,
            dispatcher=bind(toolset_for(AgentName.SOURCING), PortBundle()),
            llm=FakeLLM(),
            autonomy=AutonomyLevel.ACT,
        )
