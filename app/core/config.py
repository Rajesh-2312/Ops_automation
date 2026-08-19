"""Application settings.

Fails loudly at import time on a missing required variable. A platform that
computes payouts must never boot into a half-configured state and discover the
problem mid-cycle.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ------------------------------------------------------------------
    app_env: str = "dev"
    log_level: str = "INFO"

    # --- Supabase -------------------------------------------------------------
    supabase_url: str
    supabase_anon_key: SecretStr

    # Carries BYPASSRLS. Every code path using this key must re-check role and
    # ownership itself — the RLS policies and the column-guard triggers both
    # step aside for it. See app/core/security.py.
    supabase_service_role_key: SecretStr

    # The HS256 secret Supabase signs user access tokens with. Required by the
    # API to verify any caller — without it `app.core.security` fails closed and
    # every authenticated endpoint returns 500, which is deliberate: a verifier
    # that cannot verify must never fall back to trusting the token.
    #
    # Optional here rather than required, for the same reason as the OpenRouter
    # settings: Phase 1's console talks to Supabase directly with the user's JWT
    # and RLS does the enforcing, so the tracker is useful before the API is.
    # Dashboard: Project Settings -> API -> JWT Settings -> JWT Secret.
    supabase_jwt_secret: SecretStr | None = None

    database_url: PostgresDsn

    # --- LLM: OpenRouter is the sole gateway (CLAUDE.md §2) -------------------
    # Optional by design. CLAUDE.md §13: "Phase 1 has no AI in it. That is
    # intentional." Requiring an LLM key to boot the program tracker would make
    # the AI-free phase depend on the thing it deliberately excludes. The failure
    # is deferred to `app.core.llm.LLMClient`, which refuses to construct without
    # them — loud at the point of use rather than at import of an unrelated app.
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model_volume: str = ""
    openrouter_model_frontier: str = ""
    openrouter_app_url: str = ""
    openrouter_app_title: str = "byteXL Ops Intelligence Platform"

    @field_validator("database_url")
    @classmethod
    def _reject_transaction_pooler(cls, v: PostgresDsn) -> PostgresDsn:
        """Port 6543 is Supabase's transaction pooler, which does not preserve
        `SET ROLE` across statements.

        Every persona test impersonates a user with `SET ROLE authenticated`.
        Against the transaction pooler those tests pass while proving nothing,
        which is worse than failing. Refuse the port outright.
        """
        # `PostgresDsn` is a MultiHostUrl — Postgres connection strings may carry
        # several host:port pairs — so it exposes `.hosts()`, not `.port`. Check
        # every host: one pooler entry anywhere in the list is enough to break
        # SET ROLE for the whole connection.
        if any(host.get("port") == 6543 for host in v.hosts()):
            raise ValueError(
                "DATABASE_URL points at the transaction pooler (port 6543), which drops "
                "SET ROLE and silently invalidates every RLS persona test. "
                "Use the session pooler on port 5432."
            )
        return v

    @field_validator("app_env")
    @classmethod
    def _known_env(cls, v: str) -> str:
        allowed = {"dev", "staging", "prod"}
        if v not in allowed:
            raise ValueError(f"APP_ENV must be one of {sorted(allowed)}, got {v!r}")
        return v

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Import this, not a module-level Settings() instance, so
    tests can clear the cache and override the environment."""
    return Settings()  # type: ignore[call-arg]  # values come from env / .env
