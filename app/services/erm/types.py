"""The two vocabularies of the ERM sync task: which record, and where the card is.

CLAUDE.md §11 says "Enums in `domain/`, never string literals for status values",
and both of these belong in `app/domain/enums.py`. They are here instead, and
that is a known departure with a reason rather than an oversight — the same
reason, and the same precedent, as `app/services/comms/types.py`:

* `app/domain/enums.py` is closed to this workstream.
* `app/db/models.py` already carries eight Postgres enums with no domain
  counterpart, spelled out inline via `_pg_enum()` and typed `str`. These two
  follow that pattern, and `1900_erm_sync.sql` says so at the point of
  declaration.

**When `app/domain/enums.py` opens, move both there verbatim and re-export from
here for one release.** The values are the Postgres labels, so moving the class
is a refactor; changing a value would be a migration.

`ErmStatus` is deliberately NOT redeclared here. The record-level vocabulary —
`not_pushed`, `pending`, `synced`, `stale`, `failed` — already exists in
`app/domain/enums.py` and in `public.erm_status` (0100), already carries the
`stale` label §10 turns on, and this package imports it rather than shadowing it.

`StrEnum`, not `(str, Enum)`, for the reason `app/domain/enums.py` gives at
length: on 3.11+ the latter formats as `"ErmSyncState.QUEUED"` inside an f-string
while `==` keeps passing, so the wrong token reaches SQL silently.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "ERM_ENTITY_TABLE",
    "ErmSubjectKind",
    "ErmSyncAction",
    "ErmSyncState",
]

#: The table every audit event from this package names, so a row can be found
#: again without guessing which "id" is meant (`app/core/audit.py`).
ERM_ENTITY_TABLE: Final[str] = "erm_sync_tasks"


class ErmSubjectKind(StrEnum):
    """Which local record a sync task pushes into ERM.

    Two labels, and they are the two ERM touchpoints the legacy folder actually
    evidences rather than the two somebody might want:

    * ``PROGRAM`` — `02_Program_Planning/ERM_Program_Updates/`, workflow step 8.
    * ``TRAINER`` — `04_Trainer_Onboarding/ERM_Trainer_Updates/`, step 13.

    A third label would be a claim about a system nobody on this side of the
    integration can open. §14 says carry the open questions; it does not say
    invent labels for them.
    """

    TRAINER = "trainer"
    PROGRAM = "program"


class ErmSyncState(StrEnum):
    """Where one job card is.

    Deliberately NOT `ArtifactState`. R4's ladder governs artifacts that leave
    the building; retyping a trainer's phone number into a portal byteXL already
    holds the data for is internal record-keeping, which §8 puts at "Auto
    (internal only)" for the Onboarding agent. Borrowing the artifact vocabulary
    would imply an approval gate that does not exist and should not.

    ``STALE`` is the state §10 is actually about. It does not mean "failed": it
    means this push HAPPENED, correctly, and the local record has since moved, so
    what ERM holds is a photograph of a thing that changed. The card keeps its
    evidence — who pasted, when, the exact pack — and a fresh ``QUEUED`` card
    supersedes it.
    """

    QUEUED = "queued"
    ASSIGNED = "assigned"
    CONFIRMED = "confirmed"
    STALE = "stale"
    CANCELLED = "cancelled"


#: Every state a card can still be worked from. One card per subject may be in
#: one of these at a time — `1900_erm_sync.sql` enforces it with a partial unique
#: index so that a record edited five times in an afternoon produces one job, not
#: five.
OPEN_STATES: Final[frozenset[ErmSyncState]] = frozenset(
    {ErmSyncState.QUEUED, ErmSyncState.ASSIGNED}
)

#: States that mean "somebody pasted this once". Both carry the frozen pack and
#: the confirming human, which is why the CHECK constraints in 1900 name the pair
#: rather than `CONFIRMED` alone.
SYNCED_STATES: Final[frozenset[ErmSyncState]] = frozenset(
    {ErmSyncState.CONFIRMED, ErmSyncState.STALE}
)


class ErmSyncAction(StrEnum):
    """The audit vocabulary of this package — §11, "every state transition".

    `audit_events.action` is `text` rather than a Postgres enum (1300 argues that
    at length), so this is the source of the vocabulary rather than a gate on it,
    exactly like `app.core.audit.AuditAction`.

    There is no `released`, no `sent`, no `pushed` and there will not be. R3 is a
    rule about capability, and the honest name for what happens here is that a
    human confirms they did something in another window.
    """

    QUEUED = "erm.task_queued"
    ASSIGNED = "erm.task_assigned"
    CONFIRMED = "erm.task_confirmed"
    CANCELLED = "erm.task_cancelled"
