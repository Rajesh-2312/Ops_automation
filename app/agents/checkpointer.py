"""Graph persistence. CLAUDE.md §8: "a Postgres checkpointer so a program graph
can pause for days awaiting a human".

WHAT THE CHECKPOINTER IS ACTUALLY FOR HERE
==========================================
Not chat memory. The supervisor "runs hourly and on events" (§8) and is explicitly
"not a chatbot", so there is no conversation to remember. What it needs to
remember is a *program*: this run must know that the work order chase was drafted
at 09:00 on Tuesday and is still sitting in DRAFT on Thursday, so it neither
re-drafts it nor treats it as done. That is what "pause for days awaiting a human"
means in this system — the human step is somebody approving and sending a draft,
which happens on human time, and the graph's thread survives in Postgres across
every hourly tick in between.

**One thread per program.** `program_thread_id()` derives the thread id from the
program id, so the hourly sweep resumes each program's own state rather than
starting a fresh graph and re-deciding everything from scratch. Deriving it (as
opposed to storing a separate thread column) means no second identifier can drift
out of sync with the program it belongs to.

WHY THE SAME POSTGRES
=====================
§2 puts vectors in "pgvector, same Postgres — not a separate Chroma instance
[because] permission filtering must live in one place". Checkpoints deserve the
same argument: graph state contains program facts, and a second datastore holding
program facts is a second place to get RLS right. `DATABASE_URL` is the same
connection string the rest of the app uses.

Note that the checkpoint tables are LangGraph's own, created by
`AsyncPostgresSaver.setup()`. That is the one carve-out from §11's "migrations are
hand-authored SQL in `supabase/migrations/`, applied in filename order": the
schema belongs to a third-party library, mirroring it by hand would guarantee
drift on every upgrade, and `setup()` is idempotent. Calling it is a deployment
step, deliberately explicit rather than hidden in a first request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import get_settings

__all__ = [
    "in_memory_checkpointer",
    "postgres_checkpointer",
    "program_thread_config",
    "program_thread_id",
]

_log = structlog.get_logger(__name__)


def program_thread_id(program_id: UUID) -> str:
    """The graph thread for one program. Derived, never stored separately."""
    return f"program:{program_id}"


def program_thread_config(program_id: UUID) -> dict[str, Any]:
    """The LangGraph `config` that resumes this program's thread.

    Returned as a plain dict rather than LangGraph's `RunnableConfig` TypedDict so
    callers do not need a LangGraph import to schedule a run.
    """
    return {"configurable": {"thread_id": program_thread_id(program_id)}}


@asynccontextmanager
async def postgres_checkpointer(
    conn_string: str | None = None, *, setup: bool = False
) -> AsyncIterator[AsyncPostgresSaver]:
    """The production checkpointer, on the application's own Postgres.

    `setup=False` by default. Creating tables is a deployment action, and a
    process that quietly migrates on boot is one rolling deploy away from two
    versions racing on the same DDL. Run it once, explicitly, at release time.
    """
    settings = get_settings()
    dsn = conn_string or str(settings.database_url)
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        if setup:
            _log.info("agents.checkpointer.setup")
            await saver.setup()
        yield saver


def in_memory_checkpointer() -> InMemorySaver:
    """A non-durable checkpointer, for tests and local runs.

    Exposed here rather than imported ad hoc so that the one place a graph can be
    made non-durable is named, greppable, and obviously not the production path.
    """
    return InMemorySaver()
