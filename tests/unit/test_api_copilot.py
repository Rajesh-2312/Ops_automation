"""The Copilot HTTP surface: read-only, persona-scoped, refusals as 200s.

Four things are worth pinning about `app/api/copilot.py` and all four are here:

* **The router has no write path.** R3 and §8's read-only ceiling, asserted
  against the route table rather than trusted to review.
* **The scope handed to the Copilot is derived from the verified principal**, not
  from anything on the wire — so a client cannot ask on someone else's behalf.
* **A trainer is refused at the door** with 403 rather than with an empty answer,
  because an empty answer is indistinguishable from a corpus gap.
* **A refusal is a 200 with `answered: false`.** §9's most important behaviour is
  not an error condition, and mapping it to a 4xx would make clients retry around
  it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import copilot as copilot_api
from app.core.security import Principal, get_principal
from app.db.models import RagCorpusAccess
from app.db.session import get_session
from app.domain.enums import Corpus, Persona
from app.rag.copilot import Citation, CopilotAnswer
from app.rag.guards import Refusal, RefusalReason

COLLEGE = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")

ANSWER = CopilotAnswer(
    answered=True,
    text="A signed work order must be on file before deployment [1].",
    citations=(
        Citation(
            marker=1,
            document="Trainer Onboarding SOP",
            section="Work orders",
            corpus=Corpus.SOP,
            version=1,
            is_superseded=False,
        ),
    ),
)

REFUSAL = CopilotAnswer(
    answered=False,
    refusal=Refusal(
        reason=RefusalReason.STRUCTURED_FACT,
        message="That question asks for a stored figure; ask the tracksheet.",
    ),
)


class StubCopilot:
    def __init__(self, result: CopilotAnswer) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def answer(self, scope, question, **kwargs):
        self.calls.append({"scope": scope, "question": question, **kwargs})
        return self.result


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def execute(self, _statement):
        return FakeResult(self.rows)


@pytest.fixture
def build_client() -> Iterator:
    app = FastAPI()
    app.include_router(copilot_api.router)
    state: dict = {}

    def _build(
        persona: Persona = Persona.MANAGER,
        result: CopilotAnswer = ANSWER,
        rows=(),
    ) -> TestClient:
        principal = Principal(user_id=USER, persona=persona, college_ids=frozenset({COLLEGE}))
        stub = StubCopilot(result)
        state["copilot"] = stub
        app.dependency_overrides[get_principal] = lambda: principal
        app.dependency_overrides[copilot_api.get_copilot] = lambda: stub
        app.dependency_overrides[get_session] = lambda: FakeSession(rows)
        return TestClient(app)

    _build.state = state  # type: ignore[attr-defined]
    yield _build
    app.dependency_overrides.clear()


# --- shape of the router ------------------------------------------------------


def test_the_router_exposes_no_write_path():
    """§8 gives this agent a read-only ceiling; R3 forbids a release capability."""
    for route in copilot_api.router.routes:
        methods = getattr(route, "methods", set())
        path = getattr(route, "path", "")
        assert methods <= {"GET", "POST", "HEAD", "OPTIONS"}
        # The one POST is a question, not a mutation.
        assert path in {"/copilot/ask", "/copilot/corpora"}


def test_no_release_capable_name_appears_in_the_router(repo_root):
    """R3, checked against the source rather than trusted to review.

    `save_draft` is deliberately NOT on this list even though the Copilot has no
    draft path either: the module's docstring says so in words, and a substring
    test that forbids the token would forbid explaining why it is absent.
    """
    source = (repo_root / "app" / "api" / "copilot.py").read_text(encoding="utf-8")
    for forbidden in ("send_email", "send_whatsapp", "post_message", "mark_released"):
        assert forbidden not in source


# --- asking --------------------------------------------------------------------


def test_a_cited_answer_comes_back_with_its_citations(build_client):
    client = build_client()

    response = client.post("/copilot/ask", json={"question": "what must be on file?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answered"] is True
    assert body["citations"] == [
        {
            "marker": 1,
            "document": "Trainer Onboarding SOP",
            "section": "Work orders",
            "corpus": "sop",
            "version": 1,
            "is_superseded": False,
        }
    ]


def test_a_refusal_is_a_200_with_answered_false(build_client):
    client = build_client(result=REFUSAL)

    response = client.post("/copilot/ask", json={"question": "how many days in July?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answered"] is False
    assert body["answer"] is None
    assert body["refusal"]["reason"] == "structured_fact"


def test_the_scope_is_derived_from_the_verified_principal(build_client):
    """Not from the request body — a client cannot ask on another persona's behalf."""
    client = build_client(persona=Persona.LDE_EXECUTIVE)

    client.post("/copilot/ask", json={"question": "what is the onboarding process?"})

    scope = build_client.state["copilot"].calls[0]["scope"]
    assert scope.persona is Persona.LDE_EXECUTIVE
    assert scope.can_see_commercials is False
    assert scope.college_ids == frozenset({COLLEGE})


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
def test_external_personas_are_refused_at_the_door(build_client, persona):
    """403, not an empty answer — the latter reads as a missing document."""
    client = build_client(persona=persona)

    response = client.post("/copilot/ask", json={"question": "what does the SOP say?"})

    assert response.status_code == 403
    assert build_client.state["copilot"].calls == []


def test_unknown_body_fields_are_rejected(build_client):
    """`extra="forbid"`: a typo'd `corpus` must not silently widen the search."""
    client = build_client()
    response = client.post("/copilot/ask", json={"question": "hello there", "corpus": "sop"})
    assert response.status_code == 422


def test_the_limit_is_capped(build_client):
    client = build_client()
    response = client.post("/copilot/ask", json={"question": "hello there", "limit": 5000})
    assert response.status_code == 422


def test_requested_corpora_reach_the_copilot(build_client):
    client = build_client()

    client.post(
        "/copilot/ask",
        json={"question": "what do the contracts say?", "corpora": ["contracts"]},
    )

    assert build_client.state["copilot"].calls[0]["corpora"] == [Corpus.CONTRACTS]


# --- corpora listing ------------------------------------------------------------


def test_the_caller_sees_which_corpora_they_hold_and_why(build_client):
    """A Copilot searching four of six corpora must not look like a broken index."""
    rows = [
        RagCorpusAccess(corpus=Corpus.SOP, role=Persona.MANAGER, rationale="Policy lookup."),
        RagCorpusAccess(
            corpus=Corpus.CONTRACTS, role=Persona.MANAGER, rationale="Owns commercials."
        ),
    ]
    client = build_client(rows=rows)

    response = client.get("/copilot/corpora")

    assert response.status_code == 200
    assert response.json() == [
        {"corpus": "sop", "rationale": "Policy lookup."},
        {"corpus": "contracts", "rationale": "Owns commercials."},
    ]


def test_a_trainer_cannot_list_corpora(build_client):
    client = build_client(persona=Persona.TRAINER)
    assert client.get("/copilot/corpora").status_code == 403
