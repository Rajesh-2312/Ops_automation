"""Liveness. Deliberately does not touch the database.

A health check that queries Postgres answers a different question from the one a
load balancer asks. "Can this process serve a request?" and "is the database
reachable?" have different remedies — restart the pod versus page whoever owns
the Supabase project — and conflating them makes a database blip roll every API
instance, which turns a partial outage into a total one.

No authentication either: a liveness probe has no bearer token, and this endpoint
discloses nothing a caller could not learn from a 404.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Pydantic at the boundary even here (§11) — no raw dicts leave a handler."""

    status: Literal["ok"]
    app_env: str
    version: str


#: Mirrors `[project].version` in pyproject.toml.
API_VERSION = "0.1.0"


@router.get("/health", summary="Liveness probe")
async def health() -> HealthResponse:
    """Return 200 whenever the process is up. No I/O of any kind."""
    return HealthResponse(status="ok", app_env=get_settings().app_env, version=API_VERSION)
