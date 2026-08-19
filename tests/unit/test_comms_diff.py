"""`app/services/comms/diff.py` — the CLAUDE.md §8 review surface.

The property under test is not "difflib works". It is that the two-step —
render, then diff — makes the diff report drafter prose and nothing else. If a
substituted fact ever shows up as a hunk, the approver is being asked to review a
SQL query result, and a review surface that cries wolf gets skimmed.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.comms.diff import (
    DIFF_VERSION,
    DiffOp,
    TemplateRenderError,
    UnrenderableValueError,
    diff_from_template,
    render,
)

TEMPLATE = "Hello {{name}},\nYour July payout of INR {{net}} is approved.\nRegards,\nOps"


# --- render: step 1, the baseline ---------------------------------------------


def test_render_substitutes_every_slot() -> None:
    out = render(TEMPLATE, {"name": "VEMA PRUDHVI SAI", "net": Decimal("14035.00")})
    assert "VEMA PRUDHVI SAI" in out
    assert "INR 14035" in out
    assert "{{" not in out


def test_render_accepts_whitespace_inside_braces() -> None:
    assert render("hi {{  name }}", {"name": "Rao"}) == "hi Rao"


def test_render_refuses_a_missing_value_naming_it() -> None:
    """A blank slot reaches the recipient looking like a byteXL bug, and is
    invisible in the diff because it is missing from both sides."""
    with pytest.raises(TemplateRenderError) as exc:
        render(TEMPLATE, {"name": "Rao"})
    assert "net" in str(exc.value)


def test_render_refuses_a_float_value() -> None:
    """R7. `15584.000000000001` in a message a trainer reads is the one kind of
    wrong nobody forgives, so it is refused at the boundary, not rounded."""
    with pytest.raises(UnrenderableValueError) as exc:
        render("pay {{net}}", {"net": 15584.0})
    assert "R7" in str(exc.value)


def test_render_refuses_none_rather_than_printing_nothing() -> None:
    with pytest.raises(TemplateRenderError):
        render("hi {{name}}", {"name": None})


def test_render_keeps_decimal_out_of_exponent_notation() -> None:
    assert render("{{n}}", {"n": Decimal("15584.00")}) == "15584"
    assert render("{{n}}", {"n": Decimal("1E+4")}) == "10000"


def test_render_formats_dates_iso_and_bools_readably() -> None:
    assert render("{{d}}", {"d": dt.date(2026, 7, 26)}) == "2026-07-26"
    assert render("{{b}}", {"b": True}) == "yes"


def test_render_tolerates_extra_values_because_they_are_provenance() -> None:
    """R1: `template_values` is the record of what the drafter had in front of
    it, not merely what it printed."""
    assert render("hi {{name}}", {"name": "Rao", "pan": "BCDPK1234K"}) == "hi Rao"


def test_render_is_deterministic_which_is_what_lets_it_be_hashed() -> None:
    values = {"name": "Rao", "net": Decimal("58500")}
    assert render(TEMPLATE, values) == render(TEMPLATE, values)


# --- diff: step 2, what the approver reads ------------------------------------


def test_pure_substitution_diffs_to_identical() -> None:
    """The case that matters most. A message the drafter did not touch must cost
    the approver one glance, not a re-read."""
    values = {"name": "Rao", "net": Decimal("58500")}
    baseline = render(TEMPLATE, values)
    diff = diff_from_template(baseline, baseline)
    assert diff.identical
    assert diff.hunks == ()
    assert diff.lines_changed == 0


def test_diffing_the_raw_template_would_report_the_facts_as_changes() -> None:
    """Why step 1 exists, asserted rather than asserted-in-prose: skip the render
    and every substituted value becomes a hunk the approver must review."""
    values = {"name": "Rao", "net": Decimal("58500")}
    body = render(TEMPLATE, values)
    naive = diff_from_template(TEMPLATE, body)
    assert not naive.identical  # noise, and exactly what the two-step avoids


def test_an_added_sentence_is_one_hunk_carrying_only_the_new_line() -> None:
    baseline = "line one\nline two"
    diff = diff_from_template(baseline, "line one\nadded by the model\nline two")
    assert [h.op for h in diff.hunks] == [DiffOp.ADDED]
    assert diff.hunks[0].template == ()
    assert diff.hunks[0].message == ("added by the model",)
    assert (diff.lines_added, diff.lines_removed) == (1, 0)


def test_a_removed_line_is_a_hunk_carrying_only_the_template_side() -> None:
    diff = diff_from_template("keep\ndrop\nkeep two", "keep\nkeep two")
    assert [h.op for h in diff.hunks] == [DiffOp.REMOVED]
    assert diff.hunks[0].template == ("drop",)
    assert diff.hunks[0].message == ()


def test_a_rewritten_line_carries_both_sides() -> None:
    """A unified `-`/`+` string is trivial to render from both sides and
    impossible to recover from. The consumer is a UI and a jsonb column."""
    diff = diff_from_template("a\nold text\nc", "a\nnew text\nc")
    (hunk,) = diff.hunks
    assert hunk.op is DiffOp.CHANGED
    assert hunk.template == ("old text",)
    assert hunk.message == ("new text",)
    assert diff.lines_changed == 2  # a rewrite is two lines of reading, not one


def test_hunks_are_ordered_by_position_in_the_baseline() -> None:
    diff = diff_from_template("a\nb\nc\nd", "a\nB\nc\nD")
    assert [h.at for h in diff.hunks] == sorted(h.at for h in diff.hunks)
    assert len(diff.hunks) == 2


def test_trailing_whitespace_is_not_a_hunk() -> None:
    """A hunk an approver cannot see is a hunk they cannot act on."""
    assert diff_from_template("hello   \nworld", "hello\nworld").identical


def test_as_json_is_stamped_with_a_version() -> None:
    """A stored diff outlives the code that made it. A consumer that cannot tell
    which algorithm produced a row will eventually misread one."""
    payload = diff_from_template("a", "b").as_json()
    assert payload["version"] == DIFF_VERSION
    assert payload["identical"] is False
    assert isinstance(payload["hunks"], list)
    assert payload["hunks"][0]["op"] == DiffOp.CHANGED.value


def test_as_json_round_trips_through_plain_types_only() -> None:
    """It lands in a jsonb column, so nothing exotic may survive `as_json()`."""
    import json

    payload = diff_from_template("a\nb", "a\nc\nd").as_json()
    assert json.loads(json.dumps(payload)) == payload
