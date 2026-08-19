"""Authentication and persona behaviour, exercised rather than reasoned about.

Three claims, one test each, across every authenticated route:

* No credential -> **401**, never 500 and never 200.
* A bad credential -> **401** with one message, whatever actually failed.
* The wrong persona -> **403** with a detail a human can act on.

The third is the one worth having. `app/core/security.py` says at length why the
service must re-check persona in code: the FastAPI connection carries BYPASSRLS,
so `0700_finance.sql`'s policies never run for it and nothing in the database
stops an LDE Executive reading a net pay. That argument is only as good as the
call that tests it.
"""

from __future__ import annotations

import time

import pytest

from tests.api_contract.conftest import JULY, JWT_SECRET, Fixtures, auth, mint

pytestmark = pytest.mark.contract


def _authenticated_routes(fixtures: Fixtures) -> list[tuple[str, str, object]]:
    """Every registered route except `/health`, with a body where one is required."""
    payout = {
        "deployment_id": fixtures.deployment_65k,
        "period_start": JULY[0],
        "period_end": JULY[1],
    }
    period = {"period_start": JULY[0], "period_end": JULY[1]}
    missing = fixtures.missing_uuid
    return [
        ("GET", "/copilot/corpora", None),
        ("POST", "/copilot/ask", {"question": "What is the attendance SOP?"}),
        ("GET", "/monitoring/rules", None),
        ("GET", "/monitoring/alerts", None),
        ("GET", "/payouts?month=2026-07", None),
        ("POST", "/payouts/preview", payout),
        ("POST", "/payouts/validate", payout),
        ("POST", "/payouts/commit", payout),
        ("POST", "/payouts/remuneration-sheet.xlsx", payout),
        ("POST", "/payouts/invoice-sheet.xlsx", payout),
        ("POST", f"/programs/{fixtures.program_id}/tasks:generate", None),
        ("POST", f"/programs/{fixtures.program_id}/documents:generate", None),
        ("POST", f"/reports/programs/{fixtures.program_id}/governance", period),
        (
            "GET",
            f"/reports/programs/{fixtures.program_id}/feedback"
            f"?period_start={JULY[0]}&period_end={JULY[1]}",
            None,
        ),
        (
            "GET",
            f"/reports/colleges/{fixtures.college_id}/summary"
            f"?period_start={JULY[0]}&period_end={JULY[1]}",
            None,
        ),
        ("GET", f"/approvals/remuneration_sheets/{missing}/versions", None),
        ("POST", f"/approvals/remuneration_sheets/{missing}/submit", None),
        ("POST", f"/approvals/remuneration_sheets/{missing}/approve", None),
        ("POST", f"/approvals/remuneration_sheets/{missing}/reject", {"reason": "probe"}),
        ("POST", f"/approvals/remuneration_sheets/{missing}/release", {"notes": "probe"}),
        ("GET", f"/comms/messages?program_id={fixtures.program_id}", None),
        ("GET", f"/comms/messages/{missing}", None),
        (
            "POST",
            "/comms/messages",
            {
                "program_id": fixtures.program_id,
                "channel": "email",
                "recipient_kind": "internal_staff",
                "recipient_ref": "probe@example.invalid",
                "template_key": "probe",
                "template": "hi",
                "body": "hi",
            },
        ),
        ("PATCH", f"/comms/messages/{missing}", {"body": "amended"}),
        ("POST", f"/comms/messages/{missing}/submit", None),
        ("POST", f"/comms/messages/{missing}/approve", None),
        ("POST", f"/comms/messages/{missing}/reject", {"reason": "probe"}),
        ("POST", f"/comms/messages/{missing}/release", {"notes": "probe"}),
        ("POST", f"/comms/messages/{missing}/supersede", {"body": "replacement"}),
        ("GET", "/erm/tasks", None),
        ("GET", f"/erm/tasks/{missing}", None),
        ("POST", "/erm/tasks", {"subject_kind": "trainer", "subject_id": fixtures.trainer_id}),
        ("POST", f"/erm/tasks/{missing}/assign", {"assignee_id": fixtures.manager_id}),
        ("POST", f"/erm/tasks/{missing}/confirm", {"verified": True}),
        ("POST", f"/erm/tasks/{missing}/cancel", {"reason": "probe"}),
    ]


def test_health_needs_no_credential(client) -> None:
    """A liveness probe has no bearer token and discloses nothing."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_every_authenticated_route_401s_without_a_token(client, fixtures: Fixtures) -> None:
    routes = _authenticated_routes(fixtures)
    wrong: list[str] = []
    for method, path, body in routes:
        kwargs = {"json": body} if body is not None else {}
        response = client.request(method, path, **kwargs)
        if response.status_code != 401:
            wrong.append(f"{method} {path} -> {response.status_code} {response.text[:120]}")
    assert not wrong, "routes that did not answer 401 unauthenticated:\n" + "\n".join(wrong)


@pytest.mark.parametrize(
    "label",
    ["garbage", "alg_none", "expired", "wrong_audience", "unknown_user"],
)
def test_bad_credentials_are_401_not_500(client, fixtures: Fixtures, label: str) -> None:
    """Every way a token can be wrong lands on the same 401.

    `alg_none` is the algorithm-confusion defence: `verify_access_token` checks
    the header's `alg` against `JWT_ALGORITHMS` BEFORE handing the token to a
    verifier. `unknown_user` is `resolve_principal` refusing a token whose
    profile row is gone — "no profile" must never resolve to a usable persona.
    """
    import base64
    import json
    import uuid

    from jose import jwt

    if label == "garbage":
        token = "not.a.jwt"
    elif label == "alg_none":

        def seg(d: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

        token = f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg({'sub': str(uuid.uuid4())})}."
    elif label == "expired":
        token = jwt.encode(
            {"sub": fixtures.manager_id, "aud": "authenticated", "exp": int(time.time()) - 3600},
            JWT_SECRET,
            algorithm="HS256",
        )
    elif label == "wrong_audience":
        token = mint(fixtures.manager_id, audience="some-other-project")
    else:
        token = mint(str(uuid.uuid4()))

    response = client.get("/monitoring/rules", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401, f"{label}: {response.status_code} {response.text[:200]}"
    assert response.status_code < 500


def test_a_forged_token_signed_with_the_wrong_secret_is_refused(client, fixtures: Fixtures) -> None:
    """The signature is verified, not merely decoded."""
    from jose import jwt

    forged = jwt.encode(
        {
            "sub": fixtures.manager_id,
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
        },
        "not-the-project-secret",
        algorithm="HS256",
    )
    response = client.get("/monitoring/rules", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


# --- the commercials wall (§4, R5) --------------------------------------------

COMMERCIAL_ROUTES = [
    ("GET", "/payouts?month=2026-07", None),
    ("POST", "/payouts/preview", "payout"),
    ("POST", "/payouts/validate", "payout"),
    ("POST", "/payouts/commit", "payout"),
    ("POST", "/payouts/remuneration-sheet.xlsx", "payout"),
    ("POST", "/payouts/invoice-sheet.xlsx", "payout"),
]


@pytest.mark.parametrize("method,path,body_kind", COMMERCIAL_ROUTES)
def test_lde_executive_is_walled_off_commercials(
    client, fixtures: Fixtures, method: str, path: str, body_kind: str | None
) -> None:
    """§4: an LDE Executive gets zero commercials — in code, on a BYPASSRLS link.

    403, not an empty 200. An empty result is indistinguishable from "there is
    nothing to pay this month", which is the wrong thing for a campus executive
    to conclude about a trainer's money.
    """
    if not fixtures.lde_id:
        pytest.skip("no LDE Executive profile reaches the fixture college")
    body = (
        {
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        }
        if body_kind
        else None
    )
    kwargs = {"json": body} if body is not None else {}
    response = client.request(method, path, headers=auth(fixtures.lde_id), **kwargs)

    assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail.strip(), "403 must carry an actionable detail"


def test_a_manager_outside_the_cluster_is_refused_by_reach_not_by_persona(
    client, fixtures: Fixtures
) -> None:
    """Both conjuncts are load-bearing: the wall AND the scope (0700_finance.sql).

    A Manager clears `can_see_commercials()` and is still refused a program in
    another college, which is what `require_college_reach()` is for.
    """
    if not fixtures.program_out_of_reach:
        pytest.skip("every program in this database is inside the fixture college")
    response = client.post(
        f"/programs/{fixtures.program_out_of_reach}/tasks:generate",
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code in (403, 404), response.text
    if response.status_code == 403:
        assert "access" in response.json()["detail"].lower()


def test_an_unassigned_manager_reaches_nothing(client, fixtures: Fixtures) -> None:
    """Deny by default: no assignment row means an empty queue, not everything.

    The same answer SQL would give, reproduced in code because `my_college_ids()`
    never runs for a service-role connection.
    """
    import psycopg

    from tests.api_contract.conftest import DSN

    with psycopg.connect(DSN, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("""
            select p.id from profiles p
            where p.role in ('manager', 'senior_manager')
              and not exists (select 1 from user_college_assignments u where u.user_id = p.id)
              and not exists (select 1 from user_cluster_assignments c where c.user_id = p.id)
            limit 1
            """)
        row = cur.fetchone()
    if row is None:
        pytest.skip("every commercial persona in this database has an assignment")

    response = client.get("/payouts?month=2026-07", headers=auth(str(row[0])))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 0
    assert payload["items"] == []
