"""Drift detection — CLAUDE.md §10, the half that decides whether anyone trusts
either system next month.

    "If the local record changes after sync, flip to erm_stale and requeue.
     Without drift detection the two systems diverge within a month and neither
     is trusted."

This file is mostly a **static diff between two languages**, and that is the
point. The detector lives in `supabase/migrations/1900_erm_sync.sql` — it has to,
because §3 puts ordinary CRUD in the browser talking to Supabase directly, so a
Python detector would miss the LDE Executive fixing a phone number, which is the
common case. But the list of fields worth watching lives in
`app/services/erm/fieldpack.py`, because that is where the pack is declared.

One list, two languages, and nothing in either language can see the other. So the
test reads the migration file — it is checked in, so this needs no database and
runs in CI where a database-backed check would not — and asserts:

* every column the pack reads is compared by the trigger, and
* every column the trigger compares is read by the pack.

**Both directions matter.** Missing a column means an edit that invalidates ERM
goes unnoticed, which is the failure §10 names. Watching a spare column means
requeuing pushes nobody needed, and a queue full of work that turns out to be
unnecessary is how a detector gets switched off — which arrives at the same place
by a route that looks like diligence.

The third assertion is the one that has bitten every system with this shape: the
columns the CONFIRM path writes must NOT be watched, or every record is stale one
millisecond after every successful sync.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.erm import ErmSubjectKind, watched_columns
from app.services.erm.drift import drifted_fields, has_drifted
from app.services.erm.fieldpack import (
    PROGRAM_FIELD_ORDER,
    TRAINER_FIELD_ORDER,
    PackField,
    TrainerFacts,
    build_trainer_pack,
)

MIGRATION = "supabase/migrations/1900_erm_sync.sql"

#: Which SQL function watches which subject. Named rather than derived so that
#: renaming a trigger fails this test loudly instead of silently checking nothing.
DETECTORS: dict[ErmSubjectKind, str] = {
    ErmSubjectKind.TRAINER: "erm_trainers_detect_drift",
    ErmSubjectKind.PROGRAM: "erm_programs_detect_drift",
}

#: The columns the confirm path in `app/api/erm.py` writes, plus the one every
#: write touches. Watching any of these makes a record stale immediately after
#: being synced — see the module docstring.
SYNC_BOOKKEEPING = frozenset(
    {"erm_status", "erm_synced_at", "erm_synced_by", "erm_external_id", "erm_url", "updated_at"}
)


@pytest.fixture(scope="module")
def migration_sql(repo_root: Path) -> str:
    path = repo_root / MIGRATION
    assert path.is_file(), f"{MIGRATION} is the source of truth for the ERM schema (§11)"
    return path.read_text(encoding="utf-8")


def _function_body(sql: str, name: str) -> str:
    """The `$$ ... $$` body of one named function.

    Scoped deliberately: several functions in this migration contain
    `is distinct from`, and a whole-file regex would happily conflate the
    trainer detector with the deployments one and pass while checking nothing.
    """
    start = sql.index(f"create or replace function public.{name}()")
    end = sql.index("$$;", start)
    return sql[start:end]


def _compared_columns(body: str) -> frozenset[str]:
    """`new.x is distinct from old.x` -> {"x"}.

    Asserts the two sides name the SAME column, because `new.email is distinct
    from old.phone` is a comparison that always fires and would otherwise look
    like coverage.
    """
    pairs = re.findall(r"new\.(\w+)\s+is distinct from\s+old\.(\w+)", body)
    assert pairs, "the detector compares nothing at all"
    for left, right in pairs:
        assert left == right, f"trigger compares new.{left} against old.{right}"
    return frozenset(left for left, _ in pairs)


# --- the two-language diff -----------------------------------------------------


@pytest.mark.parametrize("kind", list(ErmSubjectKind))
def test_trigger_watches_exactly_the_pack_columns(kind: ErmSubjectKind, migration_sql: str) -> None:
    """The load-bearing assertion of the whole ERM workstream.

    If this fails, read the failure literally: either a pack field has no
    detector behind it, or the detector requeues work for a field nobody sends.
    """
    watched_in_sql = _compared_columns(_function_body(migration_sql, DETECTORS[kind]))
    assert watched_in_sql == watched_columns(kind)


@pytest.mark.parametrize("kind", list(ErmSubjectKind))
def test_sync_stamp_columns_are_never_watched(kind: ErmSubjectKind, migration_sql: str) -> None:
    """Recording a successful sync must not look like drift.

    `POST /erm/tasks/{id}/confirm` writes `erm_status`, `erm_synced_at`,
    `erm_synced_by` and `erm_external_id` on the source record, and
    `set_updated_at()` stamps `updated_at` on every write. A detector that
    watched any of them would mark the record stale in the same transaction that
    synced it — which reads as "the detector works" right up until somebody turns
    it off.
    """
    watched_in_sql = _compared_columns(_function_body(migration_sql, DETECTORS[kind]))
    assert not (watched_in_sql & SYNC_BOOKKEEPING)
    assert not (watched_columns(kind) & SYNC_BOOKKEEPING)


def test_detector_ignores_a_record_that_was_never_synced(migration_sql: str) -> None:
    """A trainer edited nine times before their first push is not stale.

    `erm_status` has a `not_pushed` label for that, and skipping the guard would
    file job cards for records nobody has decided to push yet.
    """
    for name in DETECTORS.values():
        body = _function_body(migration_sql, name)
        assert "if old.erm_synced_at is null then" in body
        assert body.index("if old.erm_synced_at is null then") < body.index("is distinct from")


# --- drift that arrives from another table -------------------------------------


def _sideways_fields() -> list[PackField]:
    return [f for f in (*TRAINER_FIELD_ORDER, *PROGRAM_FIELD_ORDER) if f.sideways]


def test_every_declared_sideways_trigger_exists(migration_sql: str) -> None:
    """ "College Assigned" is not a column on `trainers` at all.

    It is derived by walking `deployments -> batches -> programs -> colleges`, so
    a campus move drifts the ERM record without `trainers` being written to. Same
    for `colleges.name` under both packs. A field that names a sideways trigger
    which does not exist is a field nothing watches.
    """
    declared = {
        name.strip() for field in _sideways_fields() for name in (field.sideways or "").split(",")
    }
    assert declared, "no field claims sideways drift coverage — check the field orders"
    for name in declared:
        assert f"create or replace function public.{name}()" in migration_sql
        assert f"create trigger {name}" in migration_sql


def test_sideways_triggers_cover_insert_and_delete(migration_sql: str) -> None:
    """A deployment ENDING drifts the record as surely as one starting.

    An `after update` trigger alone would catch a trainer moved between batches
    and miss a trainer taken off a campus entirely, which is the more common of
    the two.
    """
    assert (
        "after insert or update or delete on public.deployments" in migration_sql
    ), "deployment inserts and deletes both change the trainer's college set"


def test_no_python_module_marks_a_record_stale(repo_root: Path) -> None:
    """Staleness has exactly one author, and it is the database.

    A second detector in Python would only see writes that went through this
    application, and §3 says most do not. Two detectors that disagree is worse
    than one that misses nothing, because the one that runs less often is the one
    people read.
    """
    from app.services import erm

    package = repo_root / "app" / "services" / "erm"
    for path in package.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for banned in ("def mark_stale", "def set_stale", "def flag_stale"):
            assert banned not in source, f"{path.name} defines a second drift detector"

    # And no transition into STALE is exported: the state exists so a row loaded
    # from Postgres can be represented, not so this application can enter it.
    assert "stale" not in " ".join(erm.__all__).lower().replace("staleness", "")


# --- explaining drift, once the database has declared it ------------------------


def _facts(**overrides: object) -> TrainerFacts:
    base = {
        "full_name": "VEMA PRUDHVI SAI",
        "pan": "BCDPV1234K",
        "email": "vema@example.com",
        "phone": "9876543210",
        "trainer_type": "freelancer",
        "work_order_status": "signed",
        "zoho_id": "ZH-1",
        "colleges": ("ABC Engineering College",),
    }
    base.update(overrides)
    return TrainerFacts(**base)  # type: ignore[arg-type]


def test_drift_names_the_field_that_moved() -> None:
    snapshot = build_trainer_pack(_facts()).source_snapshot()
    moved = build_trainer_pack(_facts(phone="9000000000"))

    drift = drifted_fields(snapshot, moved)

    assert [d.source for d in drift] == ["trainers.phone"]
    assert drift[0].was == "9876543210"
    assert drift[0].now == "9000000000"
    assert has_drifted(snapshot, moved)


def test_a_derived_field_drifts_when_the_deployment_moves() -> None:
    """The case a trainers-only detector would miss entirely."""
    snapshot = build_trainer_pack(_facts()).source_snapshot()
    moved = build_trainer_pack(_facts(colleges=("XYZ Institute of Technology",)))

    drift = drifted_fields(snapshot, moved)

    assert [d.source for d in drift] == ["deployments.colleges"]


def test_an_unconfirmed_card_reports_no_drift() -> None:
    """`None` means "never synced", not "everything changed".

    Reporting a full-width diff on a first-time push would train people to ignore
    the drift panel, which is the same failure as not having one.
    """
    assert drifted_fields(None, build_trainer_pack(_facts())) == ()
    assert not has_drifted(None, build_trainer_pack(_facts()))


def test_a_field_absent_from_the_snapshot_is_distinguishable_from_a_blank_one() -> None:
    """A pack that gained a field between syncs has never sent it.

    `was is None` says so. Collapsing that into "" would report a never-sent
    field as unchanged, which is precisely a silent divergence.
    """
    snapshot = build_trainer_pack(_facts()).source_snapshot()
    del snapshot["trainers.zoho_id"]

    drift = drifted_fields(snapshot, build_trainer_pack(_facts()))

    assert [d.source for d in drift] == ["trainers.zoho_id"]
    assert drift[0].was is None
    assert drift[0].now == "ZH-1"


def test_an_identical_pack_reports_nothing() -> None:
    pack = build_trainer_pack(_facts())
    assert drifted_fields(pack.source_snapshot(), pack) == ()
