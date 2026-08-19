"""Drift, explained — CLAUDE.md §10's "if the local record changes after sync".

    "Without drift detection the two systems diverge within a month and neither
     is trusted."

**The database decides. This module explains.**

That division is not a style preference and inverting it breaks §10. The
authority on whether a record has drifted is `supabase/migrations/1900_erm_sync.sql`:
its `BEFORE UPDATE` triggers flip `erm_status` to `stale` and its `AFTER UPDATE`
triggers file the replacement job card. They run inside the same transaction as
the edit, on every connection — PostgREST, the FastAPI service, a migration, a
psql session — and there is no write path around them.

A detector that lived here instead would only see the writes that happened to go
through this application, and CLAUDE.md §3 is explicit that most writes do not:
"the frontend talks to Supabase directly with the user's JWT for ordinary CRUD".
An LDE Executive fixing a trainer's phone number in the browser never touches
Python. Drift detection in Python would therefore be drift detection that misses
the common case, which is worse than none because it looks like coverage.

So what is this file for? **Naming the fields.** `erm_status = 'stale'` says the
record moved; it does not say what moved, and the person picking up the requeued
card needs to know whether they are retyping one phone number or the whole pack.
`erm_sync_tasks.source_snapshot` holds the values behind the confirmed pack, the
current pack is generated live, and `drifted_fields()` is the difference. It is
presentation over two structured inputs — R1 — and it decides nothing.

Consequence worth stating: this module can legitimately report **no differences**
on a task the database marked stale. A value edited and edited back, or a column
watched by the trigger but rendered identically (a phone number regaining a space
it lost), lands exactly there. That is not a bug in either half. The database is
conservative on purpose — it would rather requeue a push nobody needed than skip
one somebody did — and a screen that says "marked stale; no field differences
found" is telling the truth about a real state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.services.erm.fieldpack import FieldPack

__all__ = ["DriftedField", "drifted_fields", "has_drifted"]


@dataclass(frozen=True, slots=True)
class DriftedField:
    """One field whose value has moved since the confirmed sync.

    `was` is what the snapshot recorded at confirm time — i.e. what ERM was told
    — and `now` is what the record says today. Both are the RENDERED strings, not
    the raw columns, because the comparison that matters is the one a human would
    make holding the two packs side by side.

    `was` is `None` for a field that did not exist in the stored snapshot at all,
    which happens when the pack gains a field between one sync and the next. That
    is a real and different condition from "it was blank", and collapsing the two
    would tell somebody a field is unchanged when it has never been sent.
    """

    label: str
    source: str
    was: str | None
    now: str


def drifted_fields(
    snapshot: Mapping[str, str] | None,
    current: FieldPack,
) -> tuple[DriftedField, ...]:
    """Which fields of `current` differ from the confirmed `snapshot`.

    Keyed by `source` rather than by label, matching
    `FieldPack.source_snapshot()`: labels are provisional while ERM's own form is
    unverified, and a relabelling must not read as every field having drifted.

    `snapshot` of `None` — a task that has never been confirmed — returns empty
    rather than "everything drifted". An unconfirmed card has no sync to have
    drifted FROM, and reporting one would put a full-width red diff in front of
    somebody doing a first-time push.

    Order follows the pack, so the report reads in the same sequence as the thing
    being retyped.
    """
    if snapshot is None:
        return ()
    drifted: list[DriftedField] = []
    for entry in current.entries:
        was = snapshot.get(entry.source)
        if was != entry.value:
            drifted.append(
                DriftedField(
                    label=entry.label,
                    source=entry.source,
                    was=was,
                    now=entry.value,
                )
            )
    return tuple(drifted)


def has_drifted(snapshot: Mapping[str, str] | None, current: FieldPack) -> bool:
    """Whether any field moved. See the module docstring before using this as a gate.

    It is a convenience for a screen, not a substitute for `erm_status`. The
    database's verdict is the one that requeues work; a `False` here on a record
    the trigger marked stale is a legitimate state, not permission to clear the
    flag.
    """
    return bool(drifted_fields(snapshot, current))
