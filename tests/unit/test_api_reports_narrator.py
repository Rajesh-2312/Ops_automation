"""The narrator dependency must not refuse a report that wants no narration.

WHY THIS FILE EXISTS
====================
`/reports/*` returned 503 for EVERY request in a Phase 1 deployment, including
`include_narrative=false`, and the error message told the caller to retry
without the narrative — a path that was equally dead.

The cause was that `get_narrator` raised `HTTPException(503)` itself. FastAPI
resolves every `Depends` parameter before the handler body runs, so the raise
fired before anything could read `include_narrative`.

The reason it survived: every other test in this repo overrides
`get_narrator`, because no test may reach OpenRouter. Overriding the dependency
replaces exactly the code that was broken, so the whole suite exercised a
substitute and never the real thing. These tests therefore call `get_narrator`
DIRECTLY and never override it — that is the point, not an oversight.

CLAUDE.md §13: "Phase 1 has no AI in it. That is intentional." A tracker with
no OpenRouter configuration is the SUPPORTED deployment, not a broken one, and
its reports must serve figures — which come from the database under R1, not
from a model.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from app.api.reports import _require_narrator, get_narrator


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Phase 1 environment: no OpenRouter settings at all."""
    for name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL_VOLUME",
        "OPENROUTER_MODEL_FRONTIER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")


def test_the_dependency_does_not_raise_when_narration_is_unconfigured(
    unconfigured: None,
) -> None:
    """The regression itself.

    A raise here is a 503 on every call to all three report endpoints, whatever
    the caller asked for. Returning `None` defers the decision to the branch
    that actually wants a narrator.
    """
    assert get_narrator() is None


def test_asking_for_narration_without_it_configured_is_a_503(unconfigured: None) -> None:
    """503, not 500: the request was valid, the feature is switched off."""
    with pytest.raises(HTTPException) as raised:
        _require_narrator(get_narrator())
    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_the_503_names_the_missing_configuration(unconfigured: None) -> None:
    """ "Service unavailable" alone sends someone looking for an outage.

    The detail has to say which variable is absent, or the reader concludes the
    report service is down rather than unconfigured.
    """
    with pytest.raises(HTTPException) as raised:
        _require_narrator(get_narrator())
    assert "OPENROUTER_API_KEY" in str(raised.value.detail)


def test_the_503_points_at_the_path_that_works(unconfigured: None) -> None:
    """The old message said this too — and it was a lie, because that path 503'd.

    Keeping the advice is only honest now that the facts-only branch genuinely
    returns figures, so this asserts the advice AND the first test above are
    true together.
    """
    with pytest.raises(HTTPException) as raised:
        _require_narrator(get_narrator())
    assert "include_narrative" in str(raised.value.detail)


def test_a_configured_narrator_is_passed_straight_through() -> None:
    """`_require_narrator` must not second-guess a narrator it was handed.

    Uses a sentinel rather than a real `ReportNarrator` because this function's
    only job is the None check; constructing a real one would drag in the LLM
    client this file exists to test the absence of.
    """
    sentinel = object()
    assert _require_narrator(sentinel) is sentinel  # type: ignore[arg-type]
