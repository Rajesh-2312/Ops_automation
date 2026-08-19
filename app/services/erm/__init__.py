"""The ERM integration — CLAUDE.md §10, "manual by design".

    "ERM is external with no API. Do not build a scraper.
     Model as a sync task with a generated field pack: the system produces the
     exact field-value list in ERM's own field order, assigns it to a named
     person, they paste, they confirm. Record carries erm_synced_at,
     erm_synced_by.
     If the local record changes after sync, flip to erm_stale and requeue.
     Without drift detection the two systems diverge within a month and neither
     is trusted."

Five modules, four of them pure:

    types      the two vocabularies — which record, where the card is
    fieldpack  the ordered field-value list, and the columns that invalidate it
    drift      which fields moved since the confirmed sync — presentation only
    lifecycle  queued -> assigned -> confirmed, plus cancel. Not the R4 ladder
    models     the SQLAlchemy mapping, living here because app/db/ is closed to
               this workstream — its docstring says where it belongs

Nothing here opens a connection to ERM, and nothing may. Persistence and the
authenticated-human endpoints are `app/api/erm.py`.

FOUR PROPERTIES WORTH KNOWING BEFORE READING THE CODE
=====================================================
**Nothing transmits. There is no capability to add.** This package produces an
ordered list of labels and values for a person to read and retype in another
window. There is no HTTP client, no scraper, no browser driver, no queue of
outbound anything, and no state that means "sent". `confirm` records a human
saying they did it — a claim, attributed to a named person, which is the strongest
statement this system is entitled to make about a portal it cannot reach. R3 is
satisfied structurally rather than by policy: there is no code path from here to
ERM at all.

**The drift detector is not in Python, on purpose.** §3 puts ordinary CRUD in the
browser talking to Supabase directly, so a detector in this package would miss the
LDE Executive fixing a phone number — the common case. Staleness is set by
triggers in `supabase/migrations/1900_erm_sync.sql`, in the transaction that does
the editing, on every connection. `drift.py` explains WHICH fields moved; it never
decides THAT one did. Inverting that is the single most likely way to break §10
while appearing to implement it.

**ERM's field order is a documented guess.** Nobody here has seen ERM's form, and
the only ERM artefacts in `D:\\bytexl_Operations` are logs OF the update rather
than the update. The order is one constant in `fieldpack.py`, flagged
`FIELD_ORDER_VERIFIED = False`, versioned, stamped on every row and repeated as a
disclaimer on the API response and on screen. §14: carry the open questions, do
not invent answers.

**No pack carries money.** Not a rate, a bank rail, an IFSC or a P&L line. This
table is readable by an LDE Executive on the same terms `trainers` is, so a pack
with a day rate in it would walk a commercial value around
`can_see_commercials()` — R5 defeated by a helper function.
`tests/unit/test_erm_fieldpack.py` asserts the absence by scanning every declared
source rather than by trusting this paragraph.
"""

from __future__ import annotations

from app.services.erm.drift import DriftedField, drifted_fields, has_drifted
from app.services.erm.fieldpack import (
    BLANK_VALUE,
    FIELD_ORDER_VERIFIED,
    FIELD_ORDER_VERSION,
    PROGRAM_FIELD_ORDER,
    TRAINER_FIELD_ORDER,
    FieldPack,
    PackEntry,
    PackField,
    ProgramFacts,
    TrainerFacts,
    build_program_pack,
    build_trainer_pack,
    field_order,
    watched_columns,
)
from app.services.erm.lifecycle import (
    ErmError,
    ErmHumanActorRequiredError,
    ErmIllegalTransitionError,
    ErmOutcome,
    SyncTask,
    assign_task,
    cancel_task,
    confirm_task,
    queue_task,
)
from app.services.erm.models import ErmSyncTask
from app.services.erm.types import (
    ERM_ENTITY_TABLE,
    OPEN_STATES,
    SYNCED_STATES,
    ErmSubjectKind,
    ErmSyncAction,
    ErmSyncState,
)

__all__ = [
    "BLANK_VALUE",
    "ERM_ENTITY_TABLE",
    "FIELD_ORDER_VERIFIED",
    "FIELD_ORDER_VERSION",
    "OPEN_STATES",
    "PROGRAM_FIELD_ORDER",
    "SYNCED_STATES",
    "TRAINER_FIELD_ORDER",
    "DriftedField",
    "ErmError",
    "ErmHumanActorRequiredError",
    "ErmIllegalTransitionError",
    "ErmOutcome",
    "ErmSubjectKind",
    "ErmSyncAction",
    "ErmSyncState",
    "ErmSyncTask",
    "FieldPack",
    "PackEntry",
    "PackField",
    "ProgramFacts",
    "SyncTask",
    "TrainerFacts",
    "assign_task",
    "build_program_pack",
    "build_trainer_pack",
    "cancel_task",
    "confirm_task",
    "drifted_fields",
    "field_order",
    "has_drifted",
    "queue_task",
    "watched_columns",
]
