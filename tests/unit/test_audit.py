"""The audit trail and the R4 artifact lifecycle — CLAUDE.md §11 and R4.

Three things are pinned here and all three have bitten this codebase or its
predecessor:

* **Shape agreement across three declarations.** `app.core.audit.AuditEvent`
  (Pydantic), `app.db.models.AuditEventRow` (SQLAlchemy) and
  `supabase/migrations/1300_audit_and_artifact_versions.sql` (the source of
  truth) each state the same column list. §11 requires a test that diffs the
  models against the schema; the full `information_schema` version needs a live
  database and belongs under `tests/integration/`, but the migration file is
  checked in, so the diff can be done statically — and a static check runs in CI
  where the database-backed one does not. If they disagree, the SQL wins and the
  Python is the bug.

* **Append-only really is append-only.** An audit trail that can be edited is not
  an audit trail. The guarantee is made by grants, by the absence of policies and
  by a trigger, so the test reads the migration rather than trusting a comment.

* **The swallow is where it is supposed to be.** `write()` never raises;
  `write_within()` always does. Getting that backwards silently loses either an
  audit row or a successful business transaction, and neither failure is visible
  until someone goes looking for a record that is not there.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

import pytest
from pydantic import ValidationError

from app.core.audit import (
    AUDIT_TABLE,
    AuditAction,
    AuditEvent,
    AuditWriter,
    _row_values,
    get_audit_writer,
)
from app.db.models import ArtifactVersion, AuditEventRow
from app.domain.enums import ArtifactState, ArtifactType, Persona

MIGRATION = "supabase/migrations/1300_audit_and_artifact_versions.sql"


# --------------------------------------------------------------------------- #
# Reading the migration
# --------------------------------------------------------------------------- #
# Deliberately crude and deliberately explicit. A real SQL parser would accept
# things this schema never writes and would hide a formatting change behind a
# tolerant grammar; these two helpers only understand the shape the migration is
# actually written in, so a rewrite that breaks them is a rewrite worth noticing.

_COLUMN_RE = re.compile(
    r"^(?P<name>[a-z_]+)\s+" r"(uuid|text|jsonb|integer|timestamptz|public\.[a-z_]+)\b",
)


@pytest.fixture(scope="module")
def migration_sql(repo_root) -> str:
    path = repo_root / MIGRATION
    assert path.exists(), f"{MIGRATION} is missing — the audit table has no schema"
    return path.read_text(encoding="utf-8")


def create_table_body(sql: str, table: str) -> str:
    """The text between the parentheses of `create table public.<table> (...)`."""
    match = re.search(rf"create table public\.{table} \((?P<body>.*?)\n\);", sql, re.S)
    assert match, f"no `create table public.{table}` in {MIGRATION}"
    return match.group("body")


def sql_columns(sql: str, table: str) -> list[str]:
    """Column names of `public.<table>`, in declaration order.

    Comment lines and `constraint ...` lines are skipped; everything else must
    look like `<name> <type>` at the top indent level of the body.
    """
    columns: list[str] = []
    for raw in create_table_body(sql, table).splitlines():
        line = raw.strip()
        if not line or line.startswith("--") or line.startswith("constraint"):
            continue
        found = _COLUMN_RE.match(line)
        if found:
            columns.append(found.group("name"))
    return columns


def sql_enum_labels(sql: str, name: str) -> list[str]:
    match = re.search(rf"create type public\.{name} as enum \((?P<body>.*?)\);", sql, re.S)
    assert match, f"no `create type public.{name}` in {MIGRATION}"
    return re.findall(r"'([^']+)'", match.group("body"))


def model_columns(model: type) -> list[str]:
    return [column.name for column in model.__table__.columns]


# --------------------------------------------------------------------------- #
# §11 — the models mirror the schema
# --------------------------------------------------------------------------- #


def test_audit_events_sql_matches_the_sqlalchemy_model(migration_sql):
    """`AuditEventRow` is a mapping of the table, not a second opinion about it."""
    assert sql_columns(migration_sql, "audit_events") == model_columns(AuditEventRow)


def test_artifact_versions_sql_matches_the_sqlalchemy_model(migration_sql):
    assert sql_columns(migration_sql, "artifact_versions") == model_columns(ArtifactVersion)


def test_audit_event_model_covers_every_meaningful_column(migration_sql):
    """§11 names the fields: actor, action, before, after, at.

    The Pydantic event carries all of them plus the entity pointer. `id` is the
    one column it does not carry, because the writer generates it — see
    `_row_values`.
    """
    sql = set(sql_columns(migration_sql, "audit_events"))
    pydantic_fields = set(AuditEvent.model_fields)
    assert pydantic_fields | {"id"} == sql, (
        "app.core.audit.AuditEvent and public.audit_events have diverged; "
        "the SQL is the source of truth (CLAUDE.md §11)"
    )


def test_row_values_populates_every_column(migration_sql):
    """The INSERT names every column, so no default silently fills one in."""
    event = AuditEvent(
        actor_id=uuid.uuid4(),
        actor_persona=Persona.SENIOR_MANAGER,
        action=AuditAction.TASKS_GENERATED,
        entity_table="tasks",
        entity_id=uuid.uuid4(),
        before={"task_count": 0},
        after={"task_count": 37},
    )
    assert set(_row_values(event)) == set(sql_columns(migration_sql, "audit_events"))


# --------------------------------------------------------------------------- #
# §11 — the SQL enums mirror app/domain/enums.py
# --------------------------------------------------------------------------- #
# 0100's header: "Each value below is the exact string in app/domain/enums.py.
# [...] if the two ever disagree, Pydantic will happily serialise a value
# Postgres rejects and the failure lands at INSERT time in production rather than
# at import time in CI." The same applies to the two enums 1300 adds.


def test_artifact_state_enum_mirrors_the_domain(migration_sql):
    assert sql_enum_labels(migration_sql, "artifact_state") == [s.value for s in ArtifactState]


def test_artifact_type_enum_mirrors_the_domain(migration_sql):
    assert sql_enum_labels(migration_sql, "artifact_type") == [t.value for t in ArtifactType]


def test_artifact_state_labels_are_uppercase(migration_sql):
    """R4 spells the states in caps and Postgres enum labels are case-sensitive.

    A lowercase label here would make every insert from the app fail with
    "invalid input value for enum" — at runtime, in production, on an approval.
    """
    assert sql_enum_labels(migration_sql, "artifact_state") == [
        "DRAFT",
        "PENDING_APPROVAL",
        "APPROVED",
        "RELEASED",
    ]


def test_artifact_type_values_are_table_names(migration_sql):
    """The enum's values double as `audit_events.entity_table` (§11)."""
    for label in sql_enum_labels(migration_sql, "artifact_type"):
        assert re.search(rf"create table public\.{label}\b", migration_sql) or label in {
            "remuneration_sheets",
            "governance_reports",
            "program_documents",
        }, f"{label} is not the name of a table in this schema"


# --------------------------------------------------------------------------- #
# The append-only guarantee
# --------------------------------------------------------------------------- #


def test_audit_events_has_no_update_or_delete_policy(migration_sql):
    """No UPDATE and no DELETE policy on audit_events, for anyone — admins too.

    Written as a scan over every `create policy ... on public.audit_events` in
    the file, so adding a permissive one later fails here rather than passing a
    keyword check that only knew about the shapes present today.
    """
    policies = re.findall(
        r"create policy\s+(?P<name>\S+)\s+on public\.audit_events\s+for (?P<cmd>\w+)",
        migration_sql,
    )
    assert policies, "audit_events has no policies at all — RLS may not be configured"
    for name, command in policies:
        assert command == "select", (
            f"policy {name} grants {command.upper()} on audit_events. "
            "The table is append-only (CLAUDE.md §11) — an audit trail that can "
            "be edited is not an audit trail."
        )


def test_audit_events_grants_select_only(migration_sql):
    """A missing grant is checked before RLS is, so it is the outer of two layers."""
    assert re.search(
        r"revoke all on public\.audit_events from public, anon, authenticated;",
        migration_sql,
    ), (
        "the revoke must name `authenticated` explicitly — Supabase default "
        "privileges pre-grant every table privilege on schema public, so "
        "revoking from `public, anon` alone leaves INSERT/UPDATE/DELETE in place"
    )
    grants = re.findall(
        r"grant (?P<what>.+?) on public\.audit_events to (?P<who>\w+);", migration_sql
    )
    assert grants == [("select", "authenticated")]


def test_audit_events_blocks_update_delete_and_truncate_with_triggers(migration_sql):
    """Policies stop `authenticated`; the trigger stops everyone else.

    `app/db/session.py` connects with BYPASSRLS, so policies and grants do not
    apply to the one connection that actually writes here. TRUNCATE needs its own
    statement-level trigger — a row-level BEFORE DELETE trigger never fires for
    it, which is exactly why TRUNCATE is what someone reaches for when DELETE is
    blocked.
    """
    for event in ("update", "delete", "truncate"):
        assert re.search(
            rf"create trigger audit_events_no_{event}\s+before {event} on public\.audit_events",
            migration_sql,
        ), f"no trigger blocking {event.upper()} on audit_events"

    body = re.search(
        r"create or replace function public\.audit_events_append_only\(\).*?\$\$;",
        migration_sql,
        re.S,
    )
    assert body, "audit_events_append_only() is missing"
    assert "auth.uid() is null" not in body.group(0), (
        "the append-only guard must NOT short-circuit on a null auth.uid(): the "
        "service-role connection has one, so the guard would be inert on the "
        "only path that writes to this table"
    )


def test_artifact_versions_cannot_be_deleted_through_postgrest(migration_sql):
    """No DELETE grant — the `for all` policies would otherwise authorise it.

    Policies cannot tell the four verbs apart when written `for all`, so the
    narrowing has to happen in the GRANT. Verified against a live database
    during development: before the revoke named `authenticated`, an LDE
    Executive could delete these rows.
    """
    assert re.search(
        r"revoke all on public\.artifact_versions from public, anon, authenticated;",
        migration_sql,
    )
    grants = re.findall(
        r"grant (?P<what>.+?) on public\.artifact_versions to (?P<who>\w+);", migration_sql
    )
    assert grants == [("select, insert, update", "authenticated")]


# --------------------------------------------------------------------------- #
# R4 / §4 — the commercials wall on artifact_versions
# --------------------------------------------------------------------------- #


def test_artifact_versions_policies_use_the_shared_commercials_predicate(migration_sql):
    """§4's wall is one predicate, reused — never re-derived per policy.

    0200: "written inline it is five or six places to get wrong, and the failure
    is silent — a money policy that says `is_internal()` instead of
    `can_see_commercials()` still returns rows, still passes a smoke test, and
    leaks every trainer's rate to every campus executive."
    """
    assert "public.can_see_commercials()" in migration_sql
    # No second definition of the wall.
    assert "create or replace function public.can_see_commercials" not in migration_sql

    commercial = re.search(
        r"create policy artifact_versions_commercials_all on public\.artifact_versions"
        r".*?with check \((?P<check>.*?)\n  \);",
        migration_sql,
        re.S,
    )
    assert commercial, "the commercial policy on artifact_versions is missing"
    for conjunct in (
        "public.can_see_commercials()",
        "public.artifact_is_commercial(artifact_type, artifact_id)",
        "public.can_reach_artifact(artifact_type, artifact_id)",
    ):
        assert conjunct in commercial.group("check"), (
            f"{conjunct} is missing from the commercial WITH CHECK — both the "
            "wall and the scope conjunct are required (CLAUDE.md §4, R5)"
        )


def test_remuneration_artifacts_are_always_commercial(migration_sql):
    """An `artifact_versions` row for a remuneration sheet is money (§4, R5)."""
    fn = re.search(
        r"create or replace function public\.artifact_is_commercial.*?\$\$;",
        migration_sql,
        re.S,
    )
    assert fn, "artifact_is_commercial() is missing"
    assert re.search(r"when 'remuneration_sheets' then true", fn.group(0))
    assert re.search(r"when 'governance_reports'\s+then false", fn.group(0))
    # program_documents follows 1000's category split, and fails closed.
    assert "'remuneration', 'invoice_generation'" in fn.group(0)


def test_no_trainer_or_college_policy_on_artifact_versions(migration_sql):
    """Deny by default. §4 gives neither persona the approval lifecycle."""
    policies = re.findall(r"create policy\s+(\S+)\s+on public\.artifact_versions", migration_sql)
    assert set(policies) == {
        "artifact_versions_commercials_all",
        "artifact_versions_internal_all",
    }


def test_both_tables_force_row_level_security(migration_sql):
    """`FORCE`, not just `ENABLE`, so the table owner is subject to policy too."""
    for table in ("audit_events", "artifact_versions"):
        assert re.search(
            rf"alter table public\.{table}\s+enable row level security;", migration_sql
        ), f"{table} is missing ENABLE ROW LEVEL SECURITY"
        assert re.search(
            rf"alter table public\.{table}\s+force\s+row level security;", migration_sql
        ), f"{table} is missing FORCE ROW LEVEL SECURITY"


# --------------------------------------------------------------------------- #
# R4 — approval freezes, and one current version
# --------------------------------------------------------------------------- #


def test_approval_columns_are_all_or_nothing(migration_sql):
    """Each approval column gets its own biconditional, not one fused check.

    The fused form
        (state in ('APPROVED','RELEASED'))
        = (hash is not null and by is not null and at is not null)
    passes for a DRAFT carrying `approved_by` alone — both sides are then false.
    A draft displaying an approver who approved nothing is exactly the row this
    constraint exists to forbid. Caught against a live database; kept here so it
    cannot come back.
    """
    body = create_table_body(migration_sql, "artifact_versions")
    check = re.search(
        r"constraint artifact_versions_approved_ck check \((?P<c>.*?)\n  \),", body, re.S
    )
    assert check, "artifact_versions_approved_ck is missing"
    for column in ("approved_by", "approved_at", "content_hash"):
        assert re.search(
            rf"\(state in \('APPROVED', 'RELEASED'\)\) = \({column}\s+is not null\)",
            check.group("c"),
        ), f"{column} is not pinned in both directions by artifact_versions_approved_ck"


def test_release_is_a_separate_act_from_approval(migration_sql):
    """R4: "Approval and release are separate actions with separate audit rows"."""
    body = create_table_body(migration_sql, "artifact_versions")
    assert re.search(r"constraint artifact_versions_released_ck check", body)
    for column in ("released_by", "released_at"):
        assert re.search(rf"\(state = 'RELEASED'\) = \({column} is not null\)", body)
    # And the two acts have distinct actors and timestamps on the table.
    for column in ("approved_by", "approved_at", "released_by", "released_at"):
        assert column in sql_columns(migration_sql, "artifact_versions")


def test_exactly_one_current_version_per_artifact(migration_sql):
    """R4, enforced by a unique index rather than by application discipline."""
    assert re.search(
        r"create unique index artifact_versions_one_current\s+"
        r"on public\.artifact_versions \(artifact_type, artifact_id\)\s+"
        r"where superseded_at is null;",
        migration_sql,
    )


def test_version_numbers_are_unique_per_artifact(migration_sql):
    assert re.search(
        r"create unique index artifact_versions_unique_version\s+"
        r"on public\.artifact_versions \(artifact_type, artifact_id, version\);",
        migration_sql,
    )


def test_the_freeze_trigger_does_not_short_circuit_on_a_null_uid(migration_sql):
    """Same reasoning as the append-only guard: the writer's uid IS null.

    0200's technique 3 short-circuits column guards on `auth.uid() is null` so
    migrations and seeds pass through. These two guards must not, because the
    FastAPI service connects with BYPASSRLS and a null uid — a guard that steps
    aside for that is a guard that never runs.
    """
    fn = re.search(
        r"create or replace function public\.artifact_versions_freeze\(\).*?\$\$;",
        migration_sql,
        re.S,
    )
    assert fn, "artifact_versions_freeze() is missing"
    assert "auth.uid() is null" not in fn.group(0)
    # Deleting a decision is not a way to revise it.
    assert "cannot be deleted" in fn.group(0)


def test_the_transition_table_is_not_restated_in_sql(migration_sql):
    """`ALLOWED_TRANSITIONS` lives in the domain layer and only there.

    0700 refuses to CHECK the payout formula for the same reason: a second
    implementation of one spec, in a language with different semantics, drifts on
    its own schedule. The SQL enforces the two structural claims application code
    cannot make about itself (freeze, one-current); the state machine stays in
    `app/services/approval/`.
    """
    # PENDING_APPROVAL appears in the enum declaration and in the partial index
    # for the approval queue. It must not appear in a CHECK constraint, which is
    # where a smuggled-in state machine would live.
    checks = re.findall(r"constraint \S+ check \((.*?)\n  \),", migration_sql, re.S)
    assert checks, "no CHECK constraints found — the regex has drifted from the file"
    for check in checks:
        assert "PENDING_APPROVAL" not in check, (
            "a CHECK constraint is encoding the transition table; that belongs in "
            "app.domain.enums.ALLOWED_TRANSITIONS"
        )


# --------------------------------------------------------------------------- #
# The writer
# --------------------------------------------------------------------------- #


class _Boom(RuntimeError):
    """A persistence failure with a name that is obvious in a traceback."""


class FailingWriter(AuditWriter):
    async def _persist(self, event: AuditEvent) -> None:
        raise _Boom("database is on fire")


class RecordingWriter(AuditWriter):
    def __init__(self) -> None:
        self.persisted: list[AuditEvent] = []

    async def _persist(self, event: AuditEvent) -> None:
        self.persisted.append(event)


class FakeSession:
    """The two things `write_within` needs from an `AsyncSession`."""

    def __init__(self, *, fail: bool = False) -> None:
        self.statements: list[object] = []
        self.fail = fail
        self.committed = False

    async def execute(self, statement: object) -> None:
        if self.fail:
            raise _Boom("insert rejected")
        self.statements.append(statement)

    async def commit(self) -> None:  # pragma: no cover - must never be called
        self.committed = True


@pytest.fixture
def event() -> AuditEvent:
    return AuditEvent(
        actor_id=uuid.uuid4(),
        actor_persona=Persona.MANAGER,
        action=AuditAction.DOCUMENTS_GENERATED,
        entity_table="program_documents",
        entity_id=uuid.uuid4(),
        before={"document_count": 0},
        after={"document_count": 12},
    )


async def test_write_swallows_a_persistence_failure(event):
    """Best-effort by design, and the reasoning is in `write`'s docstring.

    Call sites invoke `write()` AFTER committing. Raising here would return a
    5xx for an operation that already succeeded and durably landed, and the
    client's retry is the expensive half of that mistake.
    """
    await FailingWriter().write(event)  # must not raise


async def test_write_still_persists_when_it_can(event):
    writer = RecordingWriter()
    await writer.write(event)
    assert writer.persisted == [event]


async def test_write_within_raises_so_the_transaction_rolls_back(event):
    """R4's half of the bargain: an unattributable approval must not commit."""
    with pytest.raises(_Boom):
        await AuditWriter().write_within(FakeSession(fail=True), event)


async def test_write_within_enqueues_without_committing(event):
    """The caller owns the transaction; committing here would defeat atomicity."""
    session = FakeSession()
    await AuditWriter().write_within(session, event)
    assert len(session.statements) == 1
    assert session.committed is False


async def test_write_within_compiles_to_an_insert_into_the_audit_table(event):
    session = FakeSession()
    await AuditWriter().write_within(session, event)
    compiled = str(session.statements[0])
    assert compiled.startswith("INSERT INTO audit_events")
    assert AUDIT_TABLE == "public.audit_events"


async def test_both_paths_emit_the_structured_log_line(event, monkeypatch):
    """The log is the only place the trail survives a database outage."""
    seen: list[AuditEvent] = []
    monkeypatch.setattr(AuditWriter, "_emit", lambda self, ev: seen.append(ev))
    await FailingWriter().write(event)
    await AuditWriter().write_within(FakeSession(), event)
    assert seen == [event, event]


def test_audit_events_are_frozen(event):
    """An event that can be edited between construction and write is not an event."""
    with pytest.raises(ValidationError):
        event.action = "something.else"


def test_get_audit_writer_is_a_singleton():
    assert get_audit_writer() is get_audit_writer()


def test_action_is_open_vocabulary(event):
    """`action` is `str`, not the enum, so another workstream need not edit this file.

    Mirrors `audit_events.action` being `text` rather than a Postgres enum —
    1300 argues that at length. The enum is the source of the vocabulary, not a
    gate on it.
    """
    custom = AuditEvent(action="payout.approved", entity_table="remuneration_sheets")
    assert custom.action == "payout.approved"
    assert AuditAction.TASKS_GENERATED == "tasks.generated"


def test_at_defaults_to_an_aware_utc_timestamp():
    """§11: "All timestamps UTC in the DB, IST at the presentation layer"."""
    now = AuditEvent(action="x", entity_table="tasks").at
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)
