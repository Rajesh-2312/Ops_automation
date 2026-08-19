"""The generated field pack — CLAUDE.md §10's "exact field-value list in ERM's
own field order".

Pure. Nothing here opens a connection, calls a model or formats a sentence. A
build function takes a frozen dataclass of values that were read from SQL and
returns the ordered list a human will retype. That is R1 in its narrowest form:
every value in a pack was passed in as structured input, and there is no path by
which one could be produced any other way.

=============================================================================
THE FIELD ORDER IS A GUESS, AND EVERY LAYER SAYS SO OUT LOUD
=============================================================================
§10 asks for ERM's OWN field order. Nobody on this side of the integration has
seen ERM's form, and the repository cannot supply it. `D:\\bytexl_Operations`
holds exactly two ERM artefacts and both are logs of the update rather than the
update:

    02_Program_Planning/ERM_Program_Updates/ERM_Program_Update_Log.xlsx
      S.No | College | Program | Batch | Updated in ERM On | Updated By |
      ERM Program ID | Verified? | Status | Remarks

    04_Trainer_Onboarding/ERM_Trainer_Updates/ERM_Trainer_Update_Log.xlsx
      S.No | Trainer Name | College Assigned | Updated in ERM On | Updated By |
      ERM Trainer ID | Verified? | Status | Remarks

Those are the columns of a tracker that RECORDS a paste. They are good evidence
of what is worth capturing about the paste — who, when, the id read back, whether
anyone verified it — and `1900_erm_sync.sql` takes its `confirmed_by`,
`confirmed_at`, `erm_external_id`, `verified` and `remarks` columns straight from
them. They are no evidence at all about the order of the fields on ERM's screen.

So the order below is **declared in one place, marked unverified, and versioned**:

* `FIELD_ORDER_VERIFIED` is `False` and is carried through the API response and
  onto the screen, so nobody can mistake the guess for a specification.
* `FIELD_ORDER_VERSION` is stamped onto every task row. When somebody finally
  opens ERM and writes the real order down, they reorder the two tuples, bump the
  version, and every pack generated under the guess stays identifiable as such.

Inventing an order and presenting it as ERM's would make the guess
unfalsifiable, which is strictly worse than an order that announces itself as
provisional. CLAUDE.md §14: carry the open questions, do not invent answers.

=============================================================================
WHAT IS DELIBERATELY NOT IN A PACK
=============================================================================
**No money.** Not a rate, not a bank account, not an IFSC, not a P&L line.
`erm_sync_tasks` is readable by an LDE Executive on the same terms `trainers` is,
so a pack carrying a day rate would walk a commercial value straight around
`can_see_commercials()` — R5, defeated by a helper. PAN is present because §6
makes it identity ("Trainer identity is PAN"), not because it is financial.
`tests/unit/test_erm_fieldpack.py` asserts the absence by scanning every declared
source rather than trusting this paragraph.

**No ERM-owned values.** `erm_external_id` is what ERM tells us, read back off
its own screen after the paste. Putting it IN the pack would ask a human to type
ERM's own id back into ERM, and — worse — would make it a watched field, so
recording the id would immediately mark the record stale.

**No computed anything.** A pack field is a value from a row, rendered. If a
future field needs arithmetic it does not belong here; §10 is a transcription
problem.

=============================================================================
THE `watched` TUPLES ARE LOAD-BEARING
=============================================================================
Each field names the columns on the subject's OWN table whose change invalidates
it. `watched_columns()` unions them, and `tests/unit/test_erm_drift.py` parses
`supabase/migrations/1900_erm_sync.sql` and asserts the migration's triggers
compare exactly that set — no more, no less. That test is the only thing keeping
one list in two languages honest, so:

* adding a field here fails the test until the trigger watches its column;
* watching a column in SQL that no field reads fails it too, because a detector
  that requeues work nobody needs is how drift detection gets switched off.

A field whose value comes from another table entirely — "College Assigned" walks
`deployments -> batches -> programs -> colleges` — has an empty `watched` and
names the migration trigger that does catch it in `sideways`. Those are the cases
§10's warning is really about: the two systems diverge through edits nobody
thinks of as edits to the record.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

from app.services.erm.types import ErmSubjectKind

__all__ = [
    "BLANK_VALUE",
    "FIELD_ORDER_VERIFIED",
    "FIELD_ORDER_VERSION",
    "PROGRAM_FIELD_ORDER",
    "TRAINER_FIELD_ORDER",
    "FieldPack",
    "PackEntry",
    "PackField",
    "ProgramFacts",
    "TrainerFacts",
    "build_program_pack",
    "build_trainer_pack",
    "field_order",
    "watched_columns",
]

#: Bumped whenever either tuple below changes ORDER or MEMBERSHIP. Stamped onto
#: `erm_sync_tasks.field_order_version`, so a pack confirmed last month is always
#: attributable to the order it was generated under. See the module docstring.
FIELD_ORDER_VERSION: Final[int] = 1

#: **The order below has never been checked against ERM's actual form.** Flip
#: this to `True` in the same commit that reorders the tuples to match a real
#: screen, and say in the commit message who looked. Until then every surface —
#: API response, screen, migration comment — repeats the disclaimer.
FIELD_ORDER_VERIFIED: Final[bool] = False

#: What a NULL renders as. Empty rather than a dash or "N/A", because a pack is
#: retyped: a human who pastes "N/A" into ERM has written the string "N/A" into a
#: system of record. `PackEntry.is_blank` is what the screen reads to say "leave
#: this field alone" — the value itself stays empty so a copy is harmless.
BLANK_VALUE: Final[str] = ""


@dataclass(frozen=True, slots=True)
class PackField:
    """One field of a pack: what ERM calls it, and where the value comes from.

    `label` is ERM's field name as best we know it — provisional, per the module
    docstring. `source` is the dotted local origin, and doubles as the key in
    `erm_sync_tasks.source_snapshot`, so a stored snapshot stays readable without
    this file.

    `watched` names the columns **on the subject's own table** whose change makes
    this field wrong. `sideways` names the migration trigger that catches drift
    arriving from another table; a field with an empty `watched` MUST have one,
    or it is a field nothing is watching.
    """

    label: str
    source: str
    watched: tuple[str, ...] = ()
    sideways: str | None = None

    def __post_init__(self) -> None:
        if not self.watched and not self.sideways:
            raise ValueError(
                f"Pack field {self.label!r} ({self.source}) is watched by nothing. "
                "Every field must name either the subject columns whose change "
                "invalidates it, or the migration trigger that catches its drift "
                "sideways — an unwatched field is how the two systems diverge "
                "(CLAUDE.md §10)."
            )


# --- the two orders -----------------------------------------------------------
#
# UNVERIFIED. See the module docstring before changing either tuple, and bump
# FIELD_ORDER_VERSION when you do.

#: Trainer details, workflow step 13 (`04_Trainer_Onboarding/ERM_Trainer_Updates/`).
#:
#: "Trainer Name" leads because it is the only field the legacy log and this pack
#: certainly share, and because it is what a human matches on in ERM's search box
#: before they can paste anything at all. Everything after it is a guess at
#: sequence, not at content — the CONTENT is defensible: these are the columns
#: `public.trainers` holds that describe the person rather than our bookkeeping
#: about them.
TRAINER_FIELD_ORDER: Final[tuple[PackField, ...]] = (
    PackField("Trainer Name", "trainers.full_name", watched=("full_name",)),
    # §6: PAN is identity, and it is the only stable key present in every legacy
    # sheet. In the pack for that reason and not as a financial detail.
    PackField("PAN", "trainers.pan", watched=("pan",)),
    PackField("Email", "trainers.email", watched=("email",)),
    PackField("Mobile", "trainers.phone", watched=("phone",)),
    PackField("Trainer Type", "trainers.type", watched=("type",)),
    PackField("Work Order Status", "trainers.work_order_status", watched=("work_order_status",)),
    PackField("ZOHO ID", "trainers.zoho_id", watched=("zoho_id",)),
    # Derived: deployments -> batches -> programs -> colleges. Not a column on
    # `trainers`, so a campus move drifts this pack without `trainers` being
    # written to at all — which is exactly the failure §10 warns about, and why
    # 1900 puts a trigger on `deployments` as well as on `trainers`.
    PackField(
        "College Assigned",
        "deployments.colleges",
        sideways="erm_deployments_detect_drift, erm_colleges_detect_drift",
    ),
)

#: Program details, workflow step 8 (`02_Program_Planning/ERM_Program_Updates/`).
#:
#: OPEN QUESTION, carried rather than answered: the legacy log's grain is
#: (College, Program, **Batch**) — it has a Batch column — while this pack's
#: subject is a program, which has many batches. Either ERM's program record is
#: per batch, or the log simply recorded which batch prompted the update. Nobody
#: here can tell, so no batch field is invented; whoever next opens ERM should
#: settle it, and if the grain turns out to be per batch this pack's subject
#: changes, not just its field list.
PROGRAM_FIELD_ORDER: Final[tuple[PackField, ...]] = (
    # Derived from `programs.college_id`. Watched here because reparenting a
    # program changes the name; the RENAME of a college is caught sideways.
    PackField(
        "College",
        "colleges.name",
        watched=("college_id",),
        sideways="erm_colleges_detect_drift",
    ),
    PackField("Program Name", "programs.name", watched=("name",)),
    PackField("Program Type", "programs.type", watched=("type",)),
    PackField("Start Date", "programs.start_date", watched=("start_date",)),
    PackField("End Date", "programs.end_date", watched=("end_date",)),
)


def field_order(kind: ErmSubjectKind) -> tuple[PackField, ...]:
    """The declared order for one subject. One lookup, no branching at call sites."""
    return TRAINER_FIELD_ORDER if kind is ErmSubjectKind.TRAINER else PROGRAM_FIELD_ORDER


def watched_columns(kind: ErmSubjectKind) -> frozenset[str]:
    """Columns on the subject's own table that the 1900 drift trigger must compare.

    The union of every field's `watched`. `tests/unit/test_erm_drift.py` parses
    the migration and asserts equality with this set in both directions — see the
    module docstring on why both directions matter.
    """
    return frozenset(column for field in field_order(kind) for column in field.watched)


# --- structured input (R1) -----------------------------------------------------
#
# A build function takes one of these and nothing else. They are frozen so a
# caller cannot mutate the facts between reading them from SQL and rendering the
# pack, and they are dataclasses rather than dicts so `no raw dicts across a
# layer boundary` (§11) holds here too.


@dataclass(frozen=True, slots=True)
class TrainerFacts:
    """Everything the trainer pack renders, as read from `public.trainers`.

    `colleges` is the derived "College Assigned" value: the distinct college
    names this trainer is deployed to, in the order the caller resolved them.
    Order is the caller's because it comes from a SQL `order by`, and re-sorting
    it here would make the snapshot disagree with the query that produced it.
    """

    full_name: str
    pan: str
    email: str | None
    phone: str | None
    trainer_type: str
    work_order_status: str
    zoho_id: str | None
    colleges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgramFacts:
    """Everything the program pack renders, as read from `public.programs`."""

    college_name: str
    name: str
    program_type: str
    start_date: dt.date | None
    end_date: dt.date | None


# --- the rendered pack ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackEntry:
    """One line of the pack: label, provenance, and the string to be retyped.

    `value` is always a string, and always the exact characters a human should
    put in ERM's box — no units, no annotations, no "(none)". `is_blank` carries
    the "there is nothing here" signal separately so a screen can say *leave this
    field alone* without the pack itself containing a word that must not be
    pasted.
    """

    label: str
    source: str
    value: str
    is_blank: bool


@dataclass(frozen=True, slots=True)
class FieldPack:
    """An ordered pack, ready to hand to a named person.

    Order is structural: `entries` is a tuple and every serialisation below keeps
    it. `1900_erm_sync.sql` stores the confirmed pack as a JSON **array** for the
    same reason — an object has no order, and the order is the deliverable.
    """

    subject_kind: ErmSubjectKind
    entries: tuple[PackEntry, ...]
    field_order_version: int = FIELD_ORDER_VERSION
    field_order_verified: bool = FIELD_ORDER_VERIFIED

    def as_json(self) -> list[dict[str, str | bool]]:
        """The array stored on `erm_sync_tasks.field_pack` at confirm time."""
        return [
            {
                "label": entry.label,
                "source": entry.source,
                "value": entry.value,
                "is_blank": entry.is_blank,
            }
            for entry in self.entries
        ]

    def source_snapshot(self) -> dict[str, str]:
        """Source path -> rendered value, for `erm_sync_tasks.source_snapshot`.

        Keyed by provenance rather than by label so it survives a relabelling: if
        ERM turns out to call it "Mobile Number", the label changes and the
        snapshots of every earlier sync stay comparable.
        """
        return {entry.source: entry.value for entry in self.entries}

    def as_paste_text(self) -> str:
        """The pack as tab-separated `label<TAB>value` lines.

        Tab-separated, and the choice has a reason. The person on the other end
        of this is working in two windows and a spreadsheet: the legacy record of
        every one of these pastes IS a spreadsheet
        (`ERM_Trainer_Update_Log.xlsx`), so a tab-separated block drops into one
        column-aligned and reads correctly in a plain textarea at the same time.

        The label travels with the value on purpose. Until the order is verified
        (see the module docstring) a bare column of values would be dangerous to
        paste — an off-by-one against ERM's real form would put a phone number in
        an email field with nothing on screen to catch it.
        """
        return "\n".join(f"{entry.label}\t{entry.value}" for entry in self.entries)


def _text(value: str | None) -> tuple[str, bool]:
    """A nullable text column, rendered. Blank-ness reported, never rendered."""
    if value is None or not value.strip():
        return BLANK_VALUE, True
    return value, False


def _date(value: dt.date | None) -> tuple[str, bool]:
    """A nullable date column, rendered ISO-8601.

    ISO because it is unambiguous and because it is what the legacy logs use
    (`2026-07-15` in the "Updated in ERM On" column). ERM's own expected input
    format is unknown like everything else about its form; if it turns out to
    want `15-Jul-2026`, this function is the one place that changes.
    """
    if value is None:
        return BLANK_VALUE, True
    return value.isoformat(), False


def _joined(values: tuple[str, ...]) -> tuple[str, bool]:
    """A derived multi-valued field, rendered as a comma-separated list.

    A trainer deployed to two colleges has two colleges, and hiding one of them
    behind a "first" would put a confidently wrong single value in front of
    somebody about to retype it.
    """
    if not values:
        return BLANK_VALUE, True
    return ", ".join(values), False


def build_trainer_pack(facts: TrainerFacts) -> FieldPack:
    """The trainer pack, in `TRAINER_FIELD_ORDER`.

    Pure: the same facts always give the same pack, which is what lets
    `tests/unit/test_erm_fieldpack.py` pin the whole thing without a database.
    """
    rendered: dict[str, tuple[str, bool]] = {
        "trainers.full_name": _text(facts.full_name),
        "trainers.pan": _text(facts.pan),
        "trainers.email": _text(facts.email),
        "trainers.phone": _text(facts.phone),
        "trainers.type": _text(facts.trainer_type),
        "trainers.work_order_status": _text(facts.work_order_status),
        "trainers.zoho_id": _text(facts.zoho_id),
        "deployments.colleges": _joined(facts.colleges),
    }
    return _assemble(ErmSubjectKind.TRAINER, rendered)


def build_program_pack(facts: ProgramFacts) -> FieldPack:
    """The program pack, in `PROGRAM_FIELD_ORDER`."""
    rendered: dict[str, tuple[str, bool]] = {
        "colleges.name": _text(facts.college_name),
        "programs.name": _text(facts.name),
        "programs.type": _text(facts.program_type),
        "programs.start_date": _date(facts.start_date),
        "programs.end_date": _date(facts.end_date),
    }
    return _assemble(ErmSubjectKind.PROGRAM, rendered)


def _assemble(kind: ErmSubjectKind, rendered: dict[str, tuple[str, bool]]) -> FieldPack:
    """Walk the declared order and pull each field's rendered value.

    Driven by the ORDER, never by the dict: a field added to the tuple with no
    renderer raises here rather than silently dropping out of the pack a human is
    about to retype. That failure mode — a pack that is quietly one field short —
    is invisible on screen and produces an ERM record that looks complete.
    """
    entries: list[PackEntry] = []
    for field in field_order(kind):
        if field.source not in rendered:
            raise KeyError(
                f"{kind.value} pack declares field {field.label!r} from "
                f"{field.source!r} but nothing renders it. A pack that is quietly "
                "one field short produces an ERM record that looks complete."
            )
        value, blank = rendered[field.source]
        entries.append(
            PackEntry(label=field.label, source=field.source, value=value, is_blank=blank)
        )
    return FieldPack(subject_kind=kind, entries=tuple(entries))
