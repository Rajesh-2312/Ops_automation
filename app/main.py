"""FastAPI application.

WHAT THIS SERVICE IS FOR
========================
Not "the backend". The frontend talks to Supabase directly with the user's JWT
for ordinary CRUD and RLS is the permission wall (CLAUDE.md §3). FastAPI exists
only for the logic RLS cannot express — checklist generation, remuneration math,
roll-ups, integrations — and it connects with the service-role key, which carries
`BYPASSRLS`.

Consequence: **every endpoint registered here re-checks persona and ownership in
code.** `app/core/security.py` explains why at length and provides the helpers.
Adding a router without those checks is not a missing nicety, it is a data leak.

Phase 1 is zero AI by design (§13), and this module imports nothing from
`app.core.llm`. An agent that cannot reliably answer "is this trainer's work
order signed?" is worse than no agent, because trust is lost once.

ROUTER REGISTRATION
-------------------
Other workstreams hand this file a router rather than editing it. Add one import
and one `include_router()` line below; keep the ordering alphabetical so a merge
conflict here is a one-line conflict.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api import (
    approvals,
    comms,
    copilot,
    erm,
    health,
    monitoring,
    payouts,
    programs,
    reports,
)
from app.core.config import Settings, get_settings
from app.db.session import dispose_engine

# NOTE FOR WINDOWS DEVELOPERS
# ---------------------------
# Do not start this app with `uvicorn app.main:app` on Windows. Use `python
# run_api.py`.
#
# Python defaults to ProactorEventLoop on Windows and psycopg's async driver
# cannot run on it: the app starts cleanly, serves /health, then fails every
# database call with `psycopg.InterfaceError`. Health-checks green, endpoints
# 500 — the worst shape of failure.
#
# The loop must be chosen where it is created, and uvicorn creates its own after
# importing this module, so nothing set here can fix it. `run_api.py` builds the
# loop itself via `asyncio.run(..., loop_factory=...)`. Setting an event loop
# POLICY would not work either, and is deprecated for removal in Python 3.16.
#
# CI never sees this: Linux does not use a proactor loop.


def configure_logging(level: str) -> None:
    """structlog to stdout, JSON in prod and human-readable elsewhere.

    §11 requires agent I/O — prompt, tools called, tokens, latency — logged for
    every invocation, and §11's audit rule requires actor/action/before/after on
    every state transition. Both are structured events, not sentences, so the
    renderer is chosen by environment rather than being pretty everywhere.

    CALL THIS ONCE, FROM THE PROCESS ENTRY POINT — NOT FROM `create_app()`
    ======================================================================
    `structlog.configure()` writes PROCESS-GLOBAL state, and two of the settings
    below are one-way doors within a process:

    * `wrapper_class=make_filtering_bound_logger(INFO)` builds a class whose
      `.debug()` is a no-op bound at class-construction time. It is not a
      renderer decision that a later handler can undo — the call is dropped
      before any processor runs.
    * `cache_logger_on_first_use=True` makes each module-level
      `structlog.get_logger()` proxy replace its own `bind` with a closure over
      the logger it assembled the FIRST time it was used. That proxy keeps the
      wrapper class and the processor list it was born with for the life of the
      process; neither `structlog.reset_defaults()` nor
      `structlog.testing.capture_logs()` reaches it afterwards.

    Both are correct for a server and wrong for anything that merely IMPORTS
    this module. While `create_app()` called this, `from app.main import
    create_app` — which is all a test does — silenced every `log.debug()` in
    `app/` for the rest of the process. `tests/rag_eval/test_logging.py` asserts
    that the copilot emits `copilot.answer.io` at DEBUG (finding F14), and it
    passed alone and failed in a full run for exactly that reason: whether the
    assertion held depended on whether some earlier test had imported
    `app.main`. That is order dependence, not a test.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=numeric)
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if get_settings().is_prod
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            # UTC in the log, as everywhere else in the system (§11).
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start-up / shut-down.

    No database connection is opened at start-up on purpose: the engine in
    `app/db/session.py` is lazy, so an unreachable database fails the first real
    request rather than preventing the process from booting and taking the
    liveness endpoint down with it.
    """
    yield
    await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Factory form so tests can construct it in isolation.

    Deliberately does NOT call `configure_logging()`; see that function's
    docstring and `__getattr__` below. Building an app object is not the same
    event as starting a process, and only the second one owns global logging.
    """
    resolved = settings or get_settings()

    app = FastAPI(
        title="byteXL Ops Intelligence Platform",
        version=health.API_VERSION,
        summary="Program lifecycle from MoU to trainer payout",
        description=(
            "Service-role API for the logic RLS cannot express. Every endpoint "
            "re-checks persona and reach in code — see app/core/security.py."
        ),
        lifespan=lifespan,
        # Interactive docs are for internal staff, not the public internet. Keep
        # them off in prod: the schema is a map of every commercial endpoint.
        docs_url=None if resolved.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_prod else "/openapi.json",
    )

    app.include_router(approvals.router)
    app.include_router(comms.router)
    app.include_router(copilot.router)
    app.include_router(erm.router)
    app.include_router(health.router)
    app.include_router(monitoring.router)
    app.include_router(payouts.router)
    app.include_router(programs.router)
    app.include_router(reports.router)

    return app


#: The ASGI application, built on first access to `app.main:app`. Not a module
#: constant, so that importing this module has no side effects at all.
_ASGI_APP: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Resolve `app.main:app`, configuring logging on the way.

    THIS IS THE PROCESS ENTRY POINT, AND THE ONLY ONE
    =================================================
    Every way this service is actually started resolves the string
    `"app.main:app"` — `run_api.py` hands it to `uvicorn.Config`, a bare
    `uvicorn app.main:app` parses it, and a gunicorn/uvicorn worker does the
    same. All of them end in `getattr(importlib.import_module("app.main"),
    "app")`, so this function runs on every production start, in the same order
    as before: `configure_logging()` first, then `create_app()`.

    What changed is what a plain `import app.main` does: nothing. It used to
    build the application AND reconfigure structlog for the whole process as an
    import side effect, which meant a test that only wanted `create_app` got the
    production log filter too, permanently — see `configure_logging()`.

    Deployment behaviour is unchanged; the ONLY difference is that a process
    which imports this module and never asks for `app` is left with structlog at
    its own defaults instead of the server's. Nothing serves traffic in that
    state.
    """
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _ASGI_APP
    if _ASGI_APP is None:
        configure_logging(get_settings().log_level)
        _ASGI_APP = create_app()
    return _ASGI_APP
