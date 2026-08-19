"""CLAUDE.md §6 regression fixtures, driven through the real HTTP endpoints.

`tests/unit/test_engine.py` proves `compute_payout()` is right. This proves the
number survives the round trip: the work order is read off the database, the
marks are counted by `app.domain.attendance`, the engine runs, and the figure is
serialised to JSON — and the rupee that comes out of `POST /payouts/preview` is
still the rupee §6 specifies.

That is a different claim from the unit test's, and it is the one a Manager
depends on. A correct engine behind a router that passes it the wrong period, the
wrong rate basis or a float would produce exactly this test's failure and none of
the unit suite's.

    | Trainer          | Terms                                    | Expected  |
    |------------------|------------------------------------------|-----------|
    | VEMA PRUDHVI SAI | bCAP ₹80,000/mo, 26–31 Jul 26, TA&DA 100 | Net 14,035 |
    | Bushily Kondala  | bCAP ₹65,000/mo, full Jul 2026           | Net 58,500 |

The two rows behind them in this database carry different names — the fixtures
are identified by their TERMS (rate, basis, period), which is what §6 actually
specifies, and never by name string. §6 is explicit that trainer identity is PAN,
never a name match.

If either figure moves, the build is broken. Do not adjust the expectation to
match the code.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.api_contract.conftest import JULY, Fixtures, auth

pytestmark = pytest.mark.contract


def test_fixture_one_net_is_14035(client, fixtures: Fixtures) -> None:
    """bCAP ₹80,000/month, 26–31 July 2026, TA&DA ₹100 -> net 14,035."""
    if not fixtures.deployment_80k:
        pytest.skip("no bCAP 80,000/month engagement on the fixture program")

    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_80k,
            "period_start": "2026-07-26",
            "period_end": "2026-07-31",
            "ta_da": "100",
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    breakdown = response.json()["breakdown"]

    assert breakdown["rate_basis"] == "per_month"
    assert Decimal(breakdown["rate"]) == Decimal("80000.00")
    assert Decimal(breakdown["payable_days"]) == Decimal("6")
    assert breakdown["days_in_month"] == 31

    # §6 quotes the sheet's whole-rupee display figures. Only `net` is rounded by
    # the engine (R6), so the intermediates are compared at display precision and
    # `net` is compared exactly.
    assert Decimal(breakdown["earned"]).quantize(Decimal("1")) == Decimal("15484")
    assert Decimal(breakdown["gross"]).quantize(Decimal("1")) == Decimal("15584")
    assert Decimal(breakdown["tds"]).quantize(Decimal("1")) == Decimal("1548")
    assert breakdown["net"] == "14035"


def test_fixture_two_net_is_58500(client, fixtures: Fixtures) -> None:
    """bCAP ₹65,000/month, the whole of July 2026 -> net 58,500."""
    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    breakdown = response.json()["breakdown"]

    assert Decimal(breakdown["rate"]) == Decimal("65000.00")
    assert Decimal(breakdown["payable_days"]) == Decimal("31")
    assert Decimal(breakdown["earned"]).quantize(Decimal("1")) == Decimal("65000")
    assert Decimal(breakdown["tds"]).quantize(Decimal("1")) == Decimal("6500")
    assert breakdown["net"] == "58500"


def test_multiply_before_divide_artifact_is_absent(client, fixtures: Fixtures) -> None:
    """§6: `65000/31*31` lands on 64999.99…; the engine must not reproduce it.

    The legacy spreadsheet carries exactly that artifact. A full-month bCAP payout
    is the case that exposes it, because dividing first and re-multiplying is the
    obvious way to write the same formula.
    """
    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    earned = Decimal(response.json()["breakdown"]["earned"])
    assert earned == Decimal("65000.00"), f"repeating-decimal artifact leaked: {earned}"


def test_tds_is_levied_on_earned_not_gross(client, fixtures: Fixtures) -> None:
    """§6: "TDS excludes reimbursements. This is why the sample sheet shows 1,548."

    With TA&DA of 100 on fixture one, TDS on gross would be 1,558.39 and TDS on
    earned is 1,548.39. The whole point of the fixture is that it is the second.
    """
    if not fixtures.deployment_80k:
        pytest.skip("no bCAP 80,000/month engagement on the fixture program")

    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_80k,
            "period_start": "2026-07-26",
            "period_end": "2026-07-31",
            "ta_da": "100",
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    b = response.json()["breakdown"]

    earned, gross, tds, rate = (
        Decimal(b["earned"]),
        Decimal(b["gross"]),
        Decimal(b["tds"]),
        Decimal(b["tds_rate"]),
    )
    assert tds == earned * rate
    assert tds != gross * rate
    assert Decimal(b["reimbursements"]) == Decimal("100")


def test_rate_per_day_is_display_only_on_the_per_month_path(client, fixtures: Fixtures) -> None:
    """R6 / §6: `rate_per_day` did not enter `earned` and must not reproduce it.

    The response documents this in prose. This asserts it, because a client that
    "checks the maths" by multiplying the two would silently disagree with the
    invoice by a few rupees every month.
    """
    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    b = response.json()["breakdown"]
    recomputed = Decimal(b["rate_per_day"]) * Decimal(b["payable_days"])
    assert recomputed != Decimal(b["earned"])


def test_every_amount_is_a_json_string_never_a_float(client, fixtures: Fixtures) -> None:
    """R7 at the wire: no rupee may reach a client as a JSON float.

    Checked on the raw body rather than the parsed one — `json.loads` would turn
    `15483.87` into a Python float and the evidence would be gone.
    """
    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    breakdown = response.json()["breakdown"]

    money_fields = (
        "rate",
        "payable_days",
        "rate_per_day",
        "earned",
        "reimbursements",
        "gross",
        "tds_rate",
        "tds",
        "deductions",
        "net_unrounded",
        "net",
    )
    for field in money_fields:
        assert isinstance(
            breakdown[field], str
        ), f"{field} arrived as {type(breakdown[field]).__name__}; R7 requires a string"
    # `days_in_month` is a count, not money, and is correctly an int.
    assert isinstance(breakdown["days_in_month"], int)


def test_json_float_on_the_request_side_is_refused(client, fixtures: Fixtures) -> None:
    """R7 inbound: `{"ta_da": 100.5}` is a 422, `{"ta_da": "100.50"}` is accepted."""
    body = {
        "deployment_id": fixtures.deployment_65k,
        "period_start": JULY[0],
        "period_end": JULY[1],
    }
    refused = client.post(
        "/payouts/preview", json={**body, "ta_da": 100.5}, headers=auth(fixtures.manager_id)
    )
    assert refused.status_code == 422, refused.text

    accepted = client.post(
        "/payouts/preview", json={**body, "ta_da": "100.50"}, headers=auth(fixtures.manager_id)
    )
    assert accepted.status_code == 200, accepted.text


def test_amount_in_words_is_rendered_and_is_not_the_legacy_name_error(
    client, fixtures: Fixtures
) -> None:
    """§6: the legacy sheet renders `#NAME?` from a missing macro. Do not reproduce."""
    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    words = response.json()["breakdown"]["net_in_words"]
    assert "#NAME" not in words
    assert "Fifty Eight Thousand Five Hundred" in words


def test_invoice_number_matches_the_documented_grammar(client, fixtures: Fixtures) -> None:
    """§6: `{PAN[0:4]}/{FY}/{MON}{seq}`, fiscal year off the PAYOUT month."""
    import re

    response = client.post(
        "/payouts/preview",
        json={
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        },
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    number = response.json()["invoice_number"]
    assert number is not None
    assert re.fullmatch(r"[A-Z]{4}/\d{2}-\d{2}/[A-Z]{3}\d+", number), number
    # July 2026 sits in FY 26-27 — April to March, derived from the payout month.
    assert "/26-27/JUL" in number
