"""Tests for app.core.config.

The transaction-pooler guard is the reason this file exists. It is a safety
feature with no runtime signal: if it silently stops working, nothing fails —
the RLS persona suite simply starts passing while proving nothing, because
`SET ROLE` does not survive Supabase's transaction pooler on port 6543.

It shipped broken. `PostgresDsn` is a `MultiHostUrl` and has no `.port`, so the
validator raised `AttributeError` on every well-formed URL instead of checking
the port. Nothing caught it, because no test had ever built `Settings` from a
real connection string. These tests are that missing coverage.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE_ENV = {
    "supabase_url": "https://example.supabase.co",
    "supabase_anon_key": "anon",
    "supabase_service_role_key": "service",
    "openrouter_api_key": "key",
    "openrouter_model_volume": "vendor/volume",
    "openrouter_model_frontier": "vendor/frontier",
}


def _settings(database_url: str, **overrides: str) -> Settings:
    # `_env_file=None` keeps these hermetic. Without it the developer's own .env
    # leaks in and decides the result — the pooler-guard test would pass or fail
    # depending on a local connection string rather than on the argument here.
    return Settings(  # type: ignore[arg-type]
        _env_file=None, **BASE_ENV, database_url=database_url, **overrides
    )


def test_session_pooler_port_is_accepted() -> None:
    s = _settings("postgresql://postgres:pw@db.example.com:5432/postgres")
    assert s.database_url.hosts()[0]["port"] == 5432


def test_transaction_pooler_port_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        _settings("postgresql://postgres:pw@db.example.com:6543/postgres")
    message = str(exc.value)
    assert "6543" in message
    assert "SET ROLE" in message


def test_transaction_pooler_refused_among_several_hosts() -> None:
    """One pooler host anywhere in the list breaks SET ROLE for the connection."""
    with pytest.raises(ValidationError):
        _settings("postgresql://postgres:pw@a.example.com:5432,b.example.com:6543/postgres")


def test_default_port_is_accepted() -> None:
    """No explicit port means the Postgres default, which is not the pooler."""
    s = _settings("postgresql://postgres:pw@db.example.com/postgres")
    assert s.database_url.hosts()[0]["port"] != 6543


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_known_app_envs(env: str) -> None:
    assert _settings("postgresql://u:p@h:5432/d", app_env=env).app_env == env


def test_unknown_app_env_is_refused() -> None:
    with pytest.raises(ValidationError):
        _settings("postgresql://u:p@h:5432/d", app_env="production")


def test_is_prod_only_for_prod() -> None:
    assert _settings("postgresql://u:p@h:5432/d", app_env="prod").is_prod
    assert not _settings("postgresql://u:p@h:5432/d", app_env="dev").is_prod


def test_secrets_are_not_printed_in_repr() -> None:
    """A settings dump reaching a log must not carry the service-role key."""
    s = _settings("postgresql://postgres:hunter2@db.example.com:5432/postgres")
    assert "service" not in repr(s.supabase_service_role_key)
    assert s.supabase_service_role_key.get_secret_value() == "service"
