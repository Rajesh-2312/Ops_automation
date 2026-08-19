"""Tests for app.core.llm.

Two things are worth pinning here.

The routing table (CLAUDE.md §2, "route by task, not by default") is a flat
`dict` precisely so it can be asserted rather than reasoned about. If a new task
is added to `LLMTask` and nobody assigns it a tier, `test_every_task_is_routed`
fails instead of the task silently defaulting to whichever model is cheapest.

The construction guard exists because the OpenRouter settings are optional —
Phase 1 has no AI in it by design (§13), so the program tracker must boot without
an LLM key. Optional settings tend to fail late and confusingly, deep inside an
HTTP call. This fails at construction, naming every missing variable.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.llm import TASK_TIER, LLMClient
from app.domain.enums import LLMTask, ModelTier

BASE_ENV = {
    "supabase_url": "https://example.supabase.co",
    "supabase_anon_key": "anon",
    "supabase_service_role_key": "service",
    "database_url": "postgresql://postgres:pw@db.example.com:5432/postgres",
}


def _settings(**llm_env: str) -> Settings:
    """Build Settings in isolation from any real `.env`.

    `_env_file=None` matters. Without it these tests read the developer's own
    `.env`, so an unrelated local value decides whether they pass — and an empty
    `OPENROUTER_API_KEY=` there yields `SecretStr('')` rather than `None`, which
    is a different object than the "unset" case being tested. A settings test
    that consults the environment it is meant to be independent of is not a test.
    """
    return Settings(_env_file=None, **BASE_ENV, **llm_env)  # type: ignore[arg-type]


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **llm_env: str) -> None:
    monkeypatch.setattr("app.core.llm.get_settings", lambda: _settings(**llm_env))


# --- routing ------------------------------------------------------------------


def test_every_task_is_routed() -> None:
    """A new LLMTask with no tier is a routing hole, not a default."""
    assert set(TASK_TIER) == set(LLMTask)


@pytest.mark.parametrize(
    ("task", "tier"),
    [
        (LLMTask.DRAFTING, ModelTier.VOLUME),
        (LLMTask.CHASE, ModelTier.VOLUME),
        (LLMTask.SUMMARY, ModelTier.VOLUME),
        (LLMTask.EXTRACTION, ModelTier.FRONTIER),
        (LLMTask.GOVERNANCE_REPORT, ModelTier.FRONTIER),
    ],
)
def test_task_routes_to_expected_tier(task: LLMTask, tier: ModelTier) -> None:
    assert TASK_TIER[task] is tier


def test_model_for_resolves_per_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch,
        openrouter_api_key="k",
        openrouter_model_volume="vendor/cheap",
        openrouter_model_frontier="vendor/strong",
    )
    client = LLMClient(client=object())  # type: ignore[arg-type]
    assert client.model_for(LLMTask.CHASE) == "vendor/cheap"
    assert client.model_for(LLMTask.EXTRACTION) == "vendor/strong"


# --- the construction guard ---------------------------------------------------


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        (
            {"openrouter_model_volume": "v", "openrouter_model_frontier": "f"},
            "OPENROUTER_API_KEY",
        ),
        (
            {"openrouter_api_key": "k", "openrouter_model_frontier": "f"},
            "OPENROUTER_MODEL_VOLUME",
        ),
        (
            {"openrouter_api_key": "k", "openrouter_model_volume": "v"},
            "OPENROUTER_MODEL_FRONTIER",
        ),
    ],
)
def test_missing_setting_is_named(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], missing: str
) -> None:
    _patch_settings(monkeypatch, **env)
    with pytest.raises(RuntimeError) as exc:
        LLMClient()
    assert missing in str(exc.value)


def test_guard_lists_all_missing_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One error naming three variables beats three rounds of trial and error."""
    _patch_settings(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        LLMClient()
    message = str(exc.value)
    for name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL_VOLUME",
        "OPENROUTER_MODEL_FRONTIER",
    ):
        assert name in message
    # The message must explain *why* they were unset, or the next reader assumes
    # a misconfiguration rather than a deliberate Phase 1 choice.
    assert "§13" in message or "Phase 1" in message


def test_phase_one_settings_build_without_any_llm_config() -> None:
    """The whole point: the AI-free program tracker boots with no LLM keys."""
    settings = _settings()
    assert settings.openrouter_api_key is None
    assert settings.openrouter_model_volume == ""
