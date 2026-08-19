"""Audit events. CLAUDE.md §11: "Every state transition writes an `AuditEvent`".

    "actor, action, before, after, at"

The table now exists: `public.audit_events`, created in
`supabase/migrations/1300_audit_and_artifact_versions.sql`. It is APPEND-ONLY —
no UPDATE policy, no DELETE policy, no UPDATE/DELETE/INSERT grant to
`authenticated`, and a trigger that rejects UPDATE, DELETE and TRUNCATE outright
so that the BYPASSRLS service-role connection this module writes on does not step
around the first two. Nothing in this file can amend a recorded event, and that
is the point.

WHY EVERY STATE TRANSITION, NOT EVERY WRITE
-------------------------------------------
An audit row answers "who changed this, from what, to what, when" during a
dispute. R4 makes that non-negotiable for the artifact lifecycle
(DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED), where approval and release
are separate actions with separate audit rows. Generating a program's checklist
is also a state transition — it is the moment a program acquires 37 obligations —
so it writes one too.

TWO WRITE PATHS, AND CHOOSING BETWEEN THEM IS A REAL DECISION
-------------------------------------------------------------
`write()` is best-effort and never raises. `write_within(session, event)` is
atomic with the caller's transaction and does raise. The previous version of this
module had only the first, with a note that the trade "is only acceptable while
`_persist()` is a stub; when the table exists, revisit it deliberately". This is
that revisit, and its reasoning is recorded on `write()` below rather than left
as a shrug.

Rule of thumb: **if losing the audit row would leave a money or approval decision
unattributable, use `write_within()`.** Everything else may use `write()`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field
from pydantic import JsonValue as JsonValue
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEventRow
from app.db.session import get_sessionmaker
from app.domain.enums import Persona

_log = structlog.get_logger(__name__)

#: The table this module writes to. Kept as a named constant because it is also
#: the grep handle that ties this file to migration 1300.
AUDIT_TABLE: Final[str] = "public.audit_events"


class AuditAction(StrEnum):
    """The actions this service currently writes.

    Deliberately narrow: it names what exists, not what might. §11 puts enums in
    `app/domain/enums.py`, and this vocabulary would normally belong there — but
    `audit_events.action` is deliberately `text` rather than a Postgres enum
    (1300 argues that at length: an enum would make "add an audit line" a schema
    change, and the predictable result of that is that somebody stops writing the
    audit line). With no SQL enum to mirror, this stays beside its only writer.

    `AuditEvent.action` is typed `str` for the same reason: another workstream
    can record its own action without editing this file, and the enum stays the
    source of the vocabulary rather than a gate on it.
    """

    TASKS_GENERATED = "tasks.generated"
    DOCUMENTS_GENERATED = "documents.generated"


class AuditEvent(BaseModel):
    """One state transition. §11: actor, action, before, after, at.

    `before` / `after` are free-form JSON row snapshots and are the one place raw
    key-value data is legitimate — they are a serialised record, not a contract
    between layers. Everything else on the event is typed.

    Frozen, because an audit row that can be edited between construction and
    write is not an audit row. The table is append-only for the same reason, so
    the property holds end to end rather than only in memory.
    """

    model_config = ConfigDict(frozen=True)

    #: Who acted. NULL only for a scheduled job with no human behind it.
    actor_id: UUID | None = None
    actor_persona: Persona | None = None

    #: What happened. See `AuditAction` for the current vocabulary.
    action: str

    #: What it happened to. `entity_table` is the Postgres table name so a row
    #: can be found again without guessing which "id" is meant.
    entity_table: str
    entity_id: UUID | None = None

    before: dict[str, JsonValue] | None = None
    after: dict[str, JsonValue] | None = None

    at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))


def _row_values(event: AuditEvent) -> dict[str, Any]:
    """The event as one `audit_events` row.

    The id is generated here rather than left to the column default so the value
    is known without a RETURNING round trip, and so the same dict works whether
    it is inserted on its own connection or enqueued in a caller's transaction.
    """
    return {
        "id": uuid.uuid4(),
        "actor_id": event.actor_id,
        "actor_persona": event.actor_persona,
        "action": event.action,
        "entity_table": event.entity_table,
        "entity_id": event.entity_id,
        "before": event.before,
        "after": event.after,
        "at": event.at,
    }


class AuditWriter:
    """Writes `AuditEvent`s. The single seam the audit trail passes through.

    Injected rather than called as a module function so a test can assert what an
    endpoint recorded without a database, and so a change of persistence strategy
    is one class body rather than a survey of call sites.
    """

    async def write(self, event: AuditEvent) -> None:
        """Record one event, best-effort. Never raises past the caller.

        THE DELIBERATE REVISIT (this docstring used to promise one)
        ----------------------------------------------------------
        The old justification was "an audit failure must not roll back the
        business transaction it is describing". That reason is now obsolete:
        `_persist()` runs on its own session and its own transaction, so it
        physically cannot roll back the caller's work. The swallow is no longer
        load-bearing for that, and keeping it on the old grounds would be
        cargo cult.

        It stays, on different and narrower grounds. Every current call site —
        `app/api/programs.py`, both generate endpoints — invokes this AFTER
        `await session.commit()`. The business change is already durable when we
        get here. Raising at that point returns a 5xx for an operation that
        succeeded; the client retries; and `tasks:generate` is idempotent but
        not everything downstream will be. We would be trading a missing audit
        row for a duplicated business effect, which is the worse of the two.
        Logging loudly and continuing is the honest response to "the thing
        happened, and we failed to write it down".

        What the swallow is NOT allowed to cover is R4. An approval or a release
        whose audit row silently vanished is precisely the failure §11 and R4
        exist to prevent, and post-hoc best-effort is not good enough for it.
        That is what `write_within()` is for: it enqueues the audit row in the
        same transaction as the state change, so the two commit together or
        neither does. Money and approval transitions must use it.

        So the choice is now explicit at each call site rather than imposed here,
        and the failure of an audit write is visible in the log under
        `audit_persist_failed` — alert on it.
        """
        self._emit(event)
        try:
            await self._persist(event)
        except Exception:
            # Deliberately broad, and deliberately terminal. See the docstring:
            # by the time we are here the business transaction has committed, and
            # there is no failure mode of THIS call that the caller can act on.
            # `structlog.exception` keeps the traceback so the log line is
            # diagnosable rather than merely alarming.
            _log.exception(
                "audit_persist_failed",
                table=AUDIT_TABLE,
                action=event.action,
                entity_table=event.entity_table,
                entity_id=str(event.entity_id) if event.entity_id else None,
                actor_id=str(event.actor_id) if event.actor_id else None,
            )

    async def write_within(self, session: AsyncSession, event: AuditEvent) -> None:
        """Record one event inside the caller's transaction. RAISES on failure.

        Use this wherever the audit row and the state change must be atomic —
        every R4 transition (submit, approve, release), every write to a money
        table, every grant of reach. The caller commits; if the INSERT fails the
        exception propagates and the state change goes with it, which is the
        correct outcome: an approval nobody can attribute is worse than an
        approval that did not happen.

        Does NOT commit. That is the caller's transaction to close, and
        committing here would defeat the atomicity this method exists for.
        """
        self._emit(event)
        await session.execute(insert(AuditEventRow).values(**_row_values(event)))

    def _emit(self, event: AuditEvent) -> None:
        """Structured log line, always, on both paths.

        Kept even now that the table exists: the log aggregator is the only place
        the trail survives a database outage, and it is where `write()`'s
        best-effort failures are reconciled from.
        """
        _log.info(
            "audit_event",
            action=event.action,
            actor_id=str(event.actor_id) if event.actor_id else None,
            actor_persona=event.actor_persona.value if event.actor_persona else None,
            entity_table=event.entity_table,
            entity_id=str(event.entity_id) if event.entity_id else None,
            before=event.before,
            after=event.after,
            at=event.at.isoformat(),
        )

    async def _persist(self, event: AuditEvent) -> None:
        """INSERT into `public.audit_events` on an independent transaction.

        Its own session, on purpose. A failure here cannot touch the caller's
        unit of work, which is what lets `write()`'s swallow be a narrow
        judgement about response codes rather than a promise about transaction
        boundaries.

        The connection carries BYPASSRLS (`app/db/session.py`), which is how this
        insert lands at all — `authenticated` holds no INSERT grant on the table,
        so a forged audit row cannot arrive through PostgREST.
        """
        factory = get_sessionmaker()
        async with factory() as session:
            await session.execute(insert(AuditEventRow).values(**_row_values(event)))
            await session.commit()


#: Default writer. Endpoints take one via `Depends(get_audit_writer)` so a test
#: can override it with `app.dependency_overrides`.
_default_writer = AuditWriter()


def get_audit_writer() -> AuditWriter:
    """FastAPI dependency returning the process-wide audit writer."""
    return _default_writer
