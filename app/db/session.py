"""Async engine and session for the FastAPI service.

⚠️  THIS CONNECTION CARRIES `BYPASSRLS`.

`DATABASE_URL` points at the Supabase Postgres with a service-role credential, so
every RLS policy and every column-guard trigger in `supabase/migrations/` steps
aside for it (`0200_identity.sql` header, techniques 2 and 3). A `select` issued
through a session from here sees **every row in the database**, including
another cluster's P&L and every trainer's rate.

Nothing in this module can fix that — it is the intended FastAPI path. The
permission check happens in `app/core/security.py`, in code, on every endpoint.
Read that module's docstring before adding a query anywhere.

The engine is created lazily and cached, so importing this module does not open a
socket and unit tests never need a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

#: SQLAlchemy needs an async DBAPI. `psycopg` (v3) is the one in our dependency
#: set and it speaks both; the URL from settings names the sync dialect, so the
#: driver is pinned here rather than in the environment — a `.env` that had to
#: spell `postgresql+psycopg://` would break `psql` and every migration script
#: that reads the same variable.
_ASYNC_DRIVER = "postgresql+psycopg"


def _async_url() -> str:
    """`DATABASE_URL` with the async driver pinned onto the scheme."""
    url = str(get_settings().database_url)
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise ValueError(f"DATABASE_URL is not a URL: {url!r}")
    return f"{_ASYNC_DRIVER}://{rest}"


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """The process-wide async engine. Cached; call `dispose_engine()` to reset."""
    return create_async_engine(
        _async_url(),
        # A stale pooled connection surfaces as a mid-request disconnect, which
        # in a payout cycle reads as data loss rather than as a network blip.
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the cached engine."""
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one session per request.

    Rolls back on any exception rather than leaving a half-applied unit of work
    behind: `POST /programs/{id}/tasks:generate` inserts 37 rows, and 20 of them
    is a worse outcome than none.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close pooled connections and drop the cached engine (app shutdown, tests)."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
