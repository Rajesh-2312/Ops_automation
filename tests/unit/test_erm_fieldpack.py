"""The generated field pack — CLAUDE.md §10's "exact field-value list in ERM's
own field order".

Five things are pinned here, and each of them is a way the pack could be wrong
without looking wrong:

* **Order is structural.** The pack is a sequence and every serialisation keeps
  it. A pack whose fields arrive in a different order on two reads is unusable
  for the one job it has.
* **The order announces that it is a guess.** Nobody has seen ERM's form.
  `FIELD_ORDER_VERIFIED` is `False` and travels with every pack, so a screen
  cannot render one without having been told.
* **R5: no pack carries money.** Asserted by scanning every declared source, not
  by reading the docstring that says so.
* **A blank is blank, and never a word.** A pack is retyped by a human; a field
  rendered as "N/A" puts the string "N/A" into a system of record.
* **A field with no renderer is a hard failure.** The silent version — a pack one
  field short — produces an ERM record that looks complete.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.erm import (
    FIELD_ORDER_VERIFIED,
    FIELD_ORDER_VERSION,
    PROGRAM_FIELD_ORDER,
    TRAINER_FIELD_ORDER,
    ErmSubjectKind,
    PackField,
    ProgramFacts,
    TrainerFacts,
    build_program_pack,
    build_trainer_pack,
    field_order,
)
from app.services.erm.fieldpack import _assemble

TRAINER = TrainerFacts(
    full_name="VEMA PRUDHVI SAI",
    pan="BCDPV1234K",
    email="vema@example.com",
    phone="9876543210",
    trainer_type="freelancer",
    work_order_status="signed",
    zoho_id="ZH-1",
    colleges=("ABC Engineering College", "XYZ Institute of Technology"),
)

PROGRAM = ProgramFacts(
    college_name="ABC Engineering College",
    name="bCAP CSE-A 2026",
    program_type="bCAP",
    start_date=dt.date(2026, 7, 1),
    end_date=dt.date(2026, 7, 31),
)


# --- order ---------------------------------------------------------------------


def test_trainer_pack_is_in_the_declared_order() -> None:
    pack = build_trainer_pack(TRAINER)
    assert [entry.label for entry in pack.entries] == [f.label for f in TRAINER_FIELD_ORDER]


def test_program_pack_is_in_the_declared_order() -> None:
    pack = build_program_pack(PROGRAM)
    assert [entry.label for entry in pack.entries] == [f.label for f in PROGRAM_FIELD_ORDER]


def test_order_survives_every_serialisation() -> None:
    """JSON array, snapshot and paste text all keep the sequence.

    `field_pack` is stored as a JSON **array** in 1900 for this reason: an object
    has no order, and the order is the deliverable.
    """
    pack = build_trainer_pack(TRAINER)
    labels = [f.label for f in TRAINER_FIELD_ORDER]

    assert [entry["label"] for entry in pack.as_json()] == labels
    assert [line.split("\t")[0] for line in pack.as_paste_text().split("\n")] == labels
    assert list(pack.source_snapshot()) == [f.source for f in TRAINER_FIELD_ORDER]


def test_paste_text_carries_the_label_beside_every_value() -> None:
    """Until the order is verified, a bare column of values is dangerous.

    An off-by-one against ERM's real form would put a phone number in an email
    field with nothing on screen to catch it.
    """
    for line in build_trainer_pack(TRAINER).as_paste_text().split("\n"):
        assert "\t" in line
        assert line.split("\t")[0]


# --- the order is a documented guess --------------------------------------------


def test_the_field_order_is_marked_unverified() -> None:
    """Flip this ONLY in the commit that reorders the tuples against a real ERM
    screen, and say in the message who looked.

    A green test here that asserted `True` would mean the codebase believes it
    knows something nobody has checked.
    """
    assert FIELD_ORDER_VERIFIED is False
    assert build_trainer_pack(TRAINER).field_order_verified is False
    assert build_program_pack(PROGRAM).field_order_verified is False


def test_every_pack_carries_the_order_version() -> None:
    """Stamped onto the row, so a pack confirmed under the guess stays attributable."""
    assert build_trainer_pack(TRAINER).field_order_version == FIELD_ORDER_VERSION
    assert build_program_pack(PROGRAM).field_order_version == FIELD_ORDER_VERSION


# --- R5 --------------------------------------------------------------------------

#: Substrings that would mean a commercial value had reached the pack. Broad on
#: purpose: a false positive costs one conversation, a false negative walks a day
#: rate around `can_see_commercials()`.
COMMERCIAL_TOKENS = (
    "rate",
    "amount",
    "bank",
    "ifsc",
    "account_number",
    "pnl",
    "invoice",
    "remuneration",
    "payout",
    "salary",
    "tds",
    "cost",
    "price",
    "work_orders",
)


@pytest.mark.parametrize("kind", list(ErmSubjectKind))
def test_no_pack_field_reads_a_commercial_column(kind: ErmSubjectKind) -> None:
    """R5, in the one place it could be defeated by a helper.

    `erm_sync_tasks` is readable by an LDE Executive on the same terms `trainers`
    is. A pack field sourced from `work_orders.rate` would therefore hand a
    persona that gets zero rows from every money table the number itself, through
    a screen built to be copied.

    PAN is present and is not a violation: §6 makes it identity ("Trainer identity
    is PAN"), and it is checked explicitly below rather than being caught by a
    token that happens not to match.
    """
    for field in field_order(kind):
        haystack = f"{field.label} {field.source}".lower()
        for token in COMMERCIAL_TOKENS:
            assert token not in haystack, f"{field.label} ({field.source}) reads {token}"


def test_pan_is_in_the_trainer_pack_as_identity() -> None:
    """Deliberate, not an oversight — §6: "Trainer identity is PAN".

    It is also what seeds the invoice number, which is why it must be right in
    both systems.
    """
    sources = [f.source for f in TRAINER_FIELD_ORDER]
    assert "trainers.pan" in sources


def test_the_erm_owned_id_is_not_a_pack_field() -> None:
    """`erm_external_id` is what ERM tells US.

    In the pack it would ask a human to type ERM's own id back into ERM, and —
    worse — it would become a watched field, so recording the id would
    immediately mark the record stale.
    """
    for kind in ErmSubjectKind:
        for field in field_order(kind):
            assert "erm_external_id" not in field.source
            assert "erm_" not in field.source


# --- rendering --------------------------------------------------------------------


def test_a_null_renders_blank_and_says_so() -> None:
    """Never "N/A", never "—". A pack is retyped, and a word gets typed."""
    pack = build_trainer_pack(
        TrainerFacts(
            full_name="A Trainer",
            pan="BCDPV1234K",
            email=None,
            phone="   ",
            trainer_type="freelancer",
            work_order_status="not_started",
            zoho_id=None,
            colleges=(),
        )
    )
    blanks = {entry.source: entry for entry in pack.entries if entry.is_blank}
    assert set(blanks) == {
        "trainers.email",
        "trainers.phone",
        "trainers.zoho_id",
        "deployments.colleges",
    }
    for entry in blanks.values():
        assert entry.value == ""


def test_dates_render_iso() -> None:
    """Unambiguous, and what the legacy update logs already use."""
    values = {entry.source: entry.value for entry in build_program_pack(PROGRAM).entries}
    assert values["programs.start_date"] == "2026-07-01"
    assert values["programs.end_date"] == "2026-07-31"


def test_a_trainer_on_two_campuses_shows_both() -> None:
    """Hiding one behind a "first" puts a confidently wrong value in front of
    somebody about to retype it."""
    values = {entry.source: entry.value for entry in build_trainer_pack(TRAINER).entries}
    assert values["deployments.colleges"] == (
        "ABC Engineering College, XYZ Institute of Technology"
    )


# --- structural guards --------------------------------------------------------------


def test_a_field_with_no_renderer_raises() -> None:
    """The silent version is a pack one field short — invisible on screen, and it
    produces an ERM record that looks complete."""
    with pytest.raises(KeyError, match="one field short"):
        _assemble(ErmSubjectKind.TRAINER, {"trainers.full_name": ("x", False)})


def test_a_field_watched_by_nothing_cannot_be_declared() -> None:
    """Every field names either the columns that invalidate it or the sideways
    trigger that catches it. An unwatched field is how the two systems diverge."""
    with pytest.raises(ValueError, match="watched by nothing"):
        PackField("Invented", "trainers.nothing")


@pytest.mark.parametrize("kind", list(ErmSubjectKind))
def test_labels_and_sources_are_unique_within_a_pack(kind: ErmSubjectKind) -> None:
    """A duplicated source would collapse two fields into one snapshot key and
    make one of them permanently undetectable as drift."""
    fields = field_order(kind)
    assert len({f.label for f in fields}) == len(fields)
    assert len({f.source for f in fields}) == len(fields)
