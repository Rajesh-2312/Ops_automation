"""Malformed, absurd and hostile input. A 500 anywhere is the finding.

The rule this file enforces is narrow and absolute: **no input a client can send
may produce a 5xx.** A 422 naming the field is a good answer, a 404 is a good
answer, a 409 explaining an illegal transition is a good answer. An unhandled
exception is never one, because it means the failure was not anticipated and the
error the caller sees was written by a stack trace rather than by a person.

`raise_server_exceptions=False` on the session client is what makes this
testable: a 500 arrives as a 500 to assert on instead of being re-raised into
the test as the original exception.
"""

from __future__ import annotations

import pytest

from tests.api_contract.conftest import JULY, Fixtures, auth

pytestmark = pytest.mark.contract


def _payout(fixtures: Fixtures, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "deployment_id": fixtures.deployment_65k,
        "period_start": JULY[0],
        "period_end": JULY[1],
    }
    body.update(overrides)
    return body


# --- request bodies -----------------------------------------------------------


def test_a_body_that_is_not_json_is_422(client, fixtures: Fixtures) -> None:
    response = client.post(
        "/payouts/preview",
        content="<<< this is not json >>>",
        headers={**auth(fixtures.manager_id), "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text


def test_an_empty_body_where_one_is_required_is_422(client, fixtures: Fixtures) -> None:
    response = client.post("/payouts/preview", headers=auth(fixtures.manager_id))
    assert response.status_code == 422, response.text


def test_missing_required_fields_are_422_and_name_themselves(client, fixtures: Fixtures) -> None:
    response = client.post(
        "/payouts/preview", json={"period_start": JULY[0]}, headers=auth(fixtures.manager_id)
    )
    assert response.status_code == 422, response.text
    locations = {".".join(str(p) for p in item["loc"]) for item in response.json()["detail"]}
    assert any("deployment_id" in loc for loc in locations), locations


def test_an_unknown_field_is_refused_not_ignored(client, fixtures: Fixtures) -> None:
    """`extra="forbid"`: a caller-supplied figure must never reach a column.

    `PayoutCommitRequest` deliberately carries no `net`. Accepting one silently —
    even to discard it — is how a client-supplied number ends up believed.
    """
    response = client.post(
        "/payouts/preview",
        json=_payout(fixtures, net="999999"),
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 422, response.text


def test_wrong_scalar_types_are_422(client, fixtures: Fixtures) -> None:
    cases = [
        _payout(fixtures, deployment_id="not-a-uuid"),
        _payout(fixtures, period_start="not-a-date"),
        _payout(fixtures, period_start=12345),
        _payout(fixtures, rate_basis="per_fortnight"),
        _payout(fixtures, ta_da=["a", "list"]),
        _payout(fixtures, tds_rate={"nested": "object"}),
    ]
    for body in cases:
        response = client.post("/payouts/preview", json=body, headers=auth(fixtures.manager_id))
        assert (
            response.status_code == 422
        ), f"{body} -> {response.status_code} {response.text[:200]}"


# --- period arithmetic --------------------------------------------------------


def test_a_period_spanning_two_months_is_refused(client, fixtures: Fixtures) -> None:
    """§6: `days_in_month` prorates a bCAP retainer and the invoice FY derives from
    the month, so a straddling period has no single correct answer."""
    response = client.post(
        "/payouts/preview",
        json=_payout(fixtures, period_end="2026-08-05"),
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 422, response.text
    assert "two months" in response.text


def test_a_reversed_period_is_refused(client, fixtures: Fixtures) -> None:
    response = client.post(
        "/payouts/preview",
        json=_payout(fixtures, period_start=JULY[1], period_end=JULY[0]),
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 422, response.text


def test_a_rate_without_its_basis_is_refused(client, fixtures: Fixtures) -> None:
    """§5 puts the basis on the work order; it is never guessed from program type."""
    response = client.post(
        "/payouts/preview", json=_payout(fixtures, rate="1000"), headers=auth(fixtures.manager_id)
    )
    assert response.status_code == 422, response.text

    response = client.post(
        "/payouts/preview",
        json=_payout(fixtures, rate_basis="per_day"),
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "month",
    ["2026-13", "2026-00", "202607", "2026-7", "not-a-month", "", "9999-99"],
)
def test_a_malformed_payout_month_is_422(client, fixtures: Fixtures, month: str) -> None:
    response = client.get(f"/payouts?month={month}", headers=auth(fixtures.manager_id))
    assert response.status_code == 422, f"month={month!r} -> {response.status_code}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F1 — GET /payouts?month=0000-01 returns 500. "
        "app/api/payouts.py:_MONTH_RE is r'^\\d{4}-(0[1-9]|1[0-2])$', which accepts the "
        "year 0000; _month_window() then calls dt.date(0, 1, 1) and Python raises "
        "'ValueError: year must be in 1..9999, not 0'. Nothing catches it, so a "
        "client-supplied query string produces an unhandled server error. "
        "Only the twelve 0000-* months are affected — 0001-01 and 9999-12 both answer "
        "200 with an empty queue. Fix: bound the year in the pattern (e.g. "
        "r'^(?!0000)\\d{4}-...'), or build the date inside the try that already raises "
        "the 422. When it is fixed this test XPASSes and strict=True fails the run, "
        "which is the signal to delete this marker."
    ),
)
def test_payout_month_year_zero_is_422_not_500(client, fixtures: Fixtures) -> None:
    """Reproduction for F1. Currently 500; must be 422."""
    response = client.get("/payouts?month=0000-01", headers=auth(fixtures.manager_id))
    assert response.status_code == 422, f"month='0000-01' -> {response.status_code}"


def test_no_payout_month_input_produces_a_5xx(client, fixtures: Fixtures) -> None:
    """The general rule, stated separately from F1's specific reproduction.

    This is the assertion that must never be relaxed: whatever a client sends as
    `month`, the answer is a 2xx or a 4xx. It currently fails on `0000-*` and is
    the reason F1 is ranked where it is.
    """
    months = [
        "2026-13",
        "2026-00",
        "202607",
        "2026-7",
        "not-a-month",
        "",
        "9999-99",
        "0001-01",
    ]
    bad = []
    for month in months:
        response = client.get(f"/payouts?month={month}", headers=auth(fixtures.manager_id))
        if response.status_code >= 500:
            bad.append(f"month={month!r} -> {response.status_code}")
    assert not bad, "months producing a server error:\n" + "\n".join(bad)


def test_a_far_future_month_is_an_empty_queue_not_an_error(client, fixtures: Fixtures) -> None:
    """A well-formed month nobody is deployed in is a correct, empty answer."""
    response = client.get("/payouts?month=9999-12", headers=auth(fixtures.manager_id))
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 0


def test_a_far_past_month_is_an_empty_queue(client, fixtures: Fixtures) -> None:
    response = client.get("/payouts?month=1900-01", headers=auth(fixtures.manager_id))
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 0


# --- absurd query parameters --------------------------------------------------


@pytest.mark.parametrize("limit", ["1000000", "-5", "0", "abc", "1e9"])
def test_absurd_limits_are_refused_or_clamped_never_5xx(
    client, fixtures: Fixtures, limit: str
) -> None:
    for path in (
        f"/comms/messages?program_id={fixtures.program_id}&limit={limit}",
        f"/erm/tasks?limit={limit}",
    ):
        response = client.get(path, headers=auth(fixtures.manager_id))
        assert response.status_code < 500, f"{path} -> {response.status_code} {response.text[:200]}"
        assert response.status_code in (200, 422), f"{path} -> {response.status_code}"


def test_absurd_copilot_limit_is_refused(client, fixtures: Fixtures) -> None:
    """`MAX_LIMIT` is a ceiling, not a suggestion — past it the model cites noise."""
    response = client.post(
        "/copilot/ask",
        json={"question": "What is the attendance SOP?", "limit": 100000},
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "qs",
    [
        "period_start=2999-01-01&period_end=2999-12-31",
        "period_start=2026-12-31&period_end=2026-01-01",
        "period_start=not-a-date",
        "program_id=not-a-uuid",
        "college_id=00000000-0000-4000-8000-000000000000",
        "as_of=not-a-timestamp",
    ],
)
def test_monitoring_alerts_survives_hostile_query_parameters(
    client, fixtures: Fixtures, qs: str
) -> None:
    response = client.get(f"/monitoring/alerts?{qs}", headers=auth(fixtures.manager_id))
    assert response.status_code < 500, f"{qs} -> {response.status_code} {response.text[:300]}"


@pytest.mark.parametrize(
    "qs",
    [
        "period_start=2026-12-31&period_end=2026-01-01",
        "period_start=1900-01-01&period_end=2999-12-31",
        "period_start=2026-07-01",
    ],
)
def test_report_periods_are_validated_not_crashed_on(client, fixtures: Fixtures, qs: str) -> None:
    response = client.get(
        f"/reports/programs/{fixtures.program_id}/feedback?{qs}",
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code < 500, f"{qs} -> {response.status_code} {response.text[:300]}"


# --- unknown ids --------------------------------------------------------------


def test_a_wellformed_but_unknown_uuid_is_404_or_403_never_500(client, fixtures: Fixtures) -> None:
    missing = fixtures.missing_uuid
    probes = [
        ("GET", f"/comms/messages/{missing}", None),
        ("GET", f"/erm/tasks/{missing}", None),
        ("POST", f"/erm/tasks/{missing}/cancel", {"reason": "probe"}),
        ("POST", f"/programs/{missing}/tasks:generate", None),
        ("POST", f"/programs/{missing}/documents:generate", None),
        ("GET", f"/approvals/remuneration_sheets/{missing}/versions", None),
        (
            "POST",
            "/payouts/preview",
            {"deployment_id": missing, "period_start": JULY[0], "period_end": JULY[1]},
        ),
    ]
    for method, path, body in probes:
        kwargs = {"json": body} if body is not None else {}
        response = client.request(method, path, headers=auth(fixtures.manager_id), **kwargs)
        assert response.status_code in (
            403,
            404,
        ), f"{method} {path} -> {response.status_code} {response.text[:200]}"


def test_a_malformed_uuid_in_the_path_is_422(client, fixtures: Fixtures) -> None:
    response = client.post("/programs/not-a-uuid/tasks:generate", headers=auth(fixtures.manager_id))
    assert response.status_code == 422, response.text


def test_an_unknown_artifact_type_is_422(client, fixtures: Fixtures) -> None:
    """`ArtifactType` is a closed vocabulary — an unlisted type cannot be approved."""
    response = client.get(
        f"/approvals/not_a_real_type/{fixtures.missing_uuid}/versions",
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 422, response.text


def test_a_reject_without_a_reason_is_refused(client, fixtures: Fixtures) -> None:
    """R4: rejection carries a stated reason. A blank one does not count."""
    for body in ({}, {"reason": ""}, {"reason": "   "}):
        response = client.post(
            f"/approvals/remuneration_sheets/{fixtures.artifact_id}/reject",
            json=body,
            headers=auth(fixtures.senior_manager_id or fixtures.manager_id),
        )
        assert response.status_code < 500, f"{body} -> {response.status_code}"
        assert response.status_code != 200, f"a blank reason was accepted: {body}"
