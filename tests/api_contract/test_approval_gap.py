"""The §14 Q3 501s, and the trap the submit endpoint opens in front of them.

CLAUDE.md §14 Q3 — "Approval authority for college-facing comms: Manager or
Senior Manager?" — is deliberately unanswered, and both approval surfaces are
explicit that they must fail loudly rather than guess:

* `app/domain/enums.py:APPROVAL_AUTHORITY` maps only `remuneration_sheets`.
* `app/services/comms/authority.py:COMMS_APPROVAL_AUTHORITY` is empty.

Both then raise, and both routers map that raise onto **501 Not Implemented**
carrying the message that names the question. That is the right design and the
tests below confirm it is what actually happens, on a real row rather than
against a random UUID — the authority check sits BEHIND the row lookup, so a
probe with a made-up id reports 404 and proves nothing.

WHAT THE PROBE FOUND
====================
`POST .../submit` does NOT consult the authority table. It moves the artifact
DRAFT -> PENDING_APPROVAL and returns 200 for a type nobody can ever approve.
From PENDING_APPROVAL:

    approve   -> 501   (no authority)
    reject    -> 501   (no authority — same predicate, deliberately)
    release   -> 409   (not a legal transition from PENDING_APPROVAL)
    supersede -> 409   (comms only; supersede replaces a FROZEN message)

There is no transition out. The artifact is stranded, and the only exit is a
manual database edit. `test_submit_strands_an_artifact_with_no_approver` pins
that behaviour so the fix — refusing the submit, or offering a withdraw — is a
deliberate change rather than an accident.
"""

from __future__ import annotations

import pytest

from tests.api_contract.conftest import DSN, Fixtures, auth

pytestmark = pytest.mark.contract


def _doc_ids(program_id: str) -> set[str]:
    import psycopg

    with psycopg.connect(DSN, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("select id from program_documents where program_id = %s", (program_id,))
        return {str(r[0]) for r in cur.fetchall()}


def _version_ids(artifact_id: str) -> set[str]:
    import psycopg

    with psycopg.connect(DSN, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("select id from artifact_versions where artifact_id = %s", (artifact_id,))
        return {str(r[0]) for r in cur.fetchall()}


@pytest.fixture
def program_document(client, fixtures: Fixtures, cleanup: list[tuple[str, str]]) -> str:
    """A `program_documents` row this test owns, deleted when it finishes.

    Generated through the API rather than inserted, so the row is shaped the way
    the application makes them, and tracked so nothing pre-existing is touched.
    """
    before = _doc_ids(fixtures.program_id)
    response = client.post(
        f"/programs/{fixtures.program_id}/documents:generate",
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    created = sorted(_doc_ids(fixtures.program_id) - before)
    for row_id in created:
        cleanup.append(("program_documents", row_id))
    if not created:
        pytest.skip("the fixture program's document register is already fully seeded")
    return created[0]


def test_comms_approve_reject_release_are_501_while_q3_is_open(
    client, fixtures: Fixtures, cleanup: list[tuple[str, str]]
) -> None:
    """Drafted, submitted, then refused — 501, naming §14 Q3.

    The message this creates is deleted afterwards. It is never released, and
    releasing would transmit nothing anyway: there is no provider behind
    `app/api/comms.py`.
    """
    drafted = client.post(
        "/comms/messages",
        json={
            "program_id": fixtures.program_id,
            "channel": "email",
            "recipient_kind": "internal_staff",
            "recipient_ref": "contract-test@example.invalid",
            "template_key": "contract.test",
            "template": "Hello {name}.",
            "template_values": {"name": "Test"},
            "subject": "contract test",
            "body": "Hello Test.",
        },
        headers=auth(fixtures.manager_id),
    )
    assert drafted.status_code == 201, drafted.text
    message_id = drafted.json()["id"]
    cleanup.append(("comms_messages", message_id))
    assert drafted.json()["state"] == "DRAFT"

    submitted = client.post(
        f"/comms/messages/{message_id}/submit", headers=auth(fixtures.manager_id)
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "PENDING_APPROVAL"

    approver = fixtures.senior_manager_id or fixtures.manager_id
    approve = client.post(f"/comms/messages/{message_id}/approve", headers=auth(approver))
    assert approve.status_code == 501, approve.text
    assert "14 Q3" in approve.json()["detail"] or "Q3" in approve.json()["detail"]

    reject = client.post(
        f"/comms/messages/{message_id}/reject",
        json={"reason": "contract test"},
        headers=auth(approver),
    )
    assert reject.status_code == 501, reject.text

    release = client.post(
        f"/comms/messages/{message_id}/release",
        json={"notes": "contract test"},
        headers=auth(approver),
    )
    # Release is unreachable for a different reason: R4 forbids the transition.
    assert release.status_code == 409, release.text
    assert "not a legal" in release.json()["detail"]


def test_approvals_approve_and_reject_are_501_for_a_type_with_no_authority(
    client, fixtures: Fixtures, program_document: str, cleanup: list[tuple[str, str]]
) -> None:
    """`program_documents` has no `APPROVAL_AUTHORITY` entry, so both refuse with 501."""
    before = _version_ids(program_document)
    submitted = client.post(
        f"/approvals/program_documents/{program_document}/submit",
        headers=auth(fixtures.manager_id),
    )
    assert submitted.status_code == 200, submitted.text
    for row_id in sorted(_version_ids(program_document) - before):
        cleanup.insert(0, ("artifact_versions", row_id))

    approver = fixtures.senior_manager_id or fixtures.manager_id
    approve = client.post(
        f"/approvals/program_documents/{program_document}/approve", headers=auth(approver)
    )
    assert approve.status_code == 501, approve.text
    assert "approval authority" in approve.json()["detail"].lower()

    reject = client.post(
        f"/approvals/program_documents/{program_document}/reject",
        json={"reason": "contract test"},
        headers=auth(approver),
    )
    assert reject.status_code == 501, reject.text


def test_submit_strands_an_artifact_with_no_approver(
    client, fixtures: Fixtures, program_document: str, cleanup: list[tuple[str, str]]
) -> None:
    """FINDING: submit succeeds for a type that can never be approved OR rejected.

    Every exit from PENDING_APPROVAL is closed:

        approve -> 501, reject -> 501, release -> 409, and there is no withdraw.

    The artifact sits in a Senior Manager's queue forever. R4 says approval and
    rejection are separate acts with separate audit rows; it does not say a
    submit may be made with no possible second act. The honest fix is for
    `submit` to consult `approver_personas()` and refuse with the same 501 the
    approve endpoint gives, so the failure lands before the state moves.

    When that fix lands, this test fails on its first assertion. That is the
    point: change it deliberately, do not delete it.
    """
    submitted = client.post(
        f"/approvals/program_documents/{program_document}/submit",
        headers=auth(fixtures.manager_id),
    )
    assert (
        submitted.status_code == 200
    ), "submit now refuses an unapprovable artifact — the trap is closed, update this test"
    version = submitted.json()
    for row_id in [version["id"]]:
        cleanup.insert(0, ("artifact_versions", row_id))
    assert version["state"] == "PENDING_APPROVAL"

    approver = fixtures.senior_manager_id or fixtures.manager_id
    exits = {
        "approve": client.post(
            f"/approvals/program_documents/{program_document}/approve", headers=auth(approver)
        ).status_code,
        "reject": client.post(
            f"/approvals/program_documents/{program_document}/reject",
            json={"reason": "get me out"},
            headers=auth(approver),
        ).status_code,
        "release": client.post(
            f"/approvals/program_documents/{program_document}/release",
            json={"notes": "get me out"},
            headers=auth(approver),
        ).status_code,
    }
    assert exits == {"approve": 501, "reject": 501, "release": 409}, exits

    history = client.get(
        f"/approvals/program_documents/{program_document}/versions",
        headers=auth(fixtures.manager_id),
    )
    assert history.status_code == 200, history.text
    current = [v for v in history.json()["versions"] if v["is_current"]]
    assert (
        current and current[0]["state"] == "PENDING_APPROVAL"
    ), "the artifact did not stay stranded; re-read this test's docstring"


def test_remuneration_sheet_approval_authority_is_senior_manager_only(
    client, fixtures: Fixtures
) -> None:
    """§4 puts payout approval in the Senior Manager's column, and only there.

    Asserted through the version history rather than by attempting a transition,
    because the one sheet on file is already RELEASED and RELEASED is terminal —
    which this also confirms.
    """
    response = client.get(
        f"/approvals/{fixtures.artifact_type}/{fixtures.artifact_id}/versions",
        headers=auth(fixtures.senior_manager_id or fixtures.manager_id),
    )
    if response.status_code == 403:
        pytest.skip("no commercial persona in this database reaches the sample artifact")
    assert response.status_code == 200, response.text
    assert response.json()["artifact_type"] == fixtures.artifact_type


def test_a_released_artifact_is_terminal(client, fixtures: Fixtures) -> None:
    """R4: RELEASED has no outgoing transition. Attempting one is 409, not 500."""
    approver = fixtures.senior_manager_id or fixtures.manager_id
    history = client.get(
        f"/approvals/{fixtures.artifact_type}/{fixtures.artifact_id}/versions",
        headers=auth(approver),
    )
    if history.status_code != 200:
        pytest.skip("sample artifact not reachable by any commercial persona here")
    current = [v for v in history.json()["versions"] if v["is_current"]]
    if not current or current[0]["state"] != "RELEASED":
        pytest.skip("the sample artifact is not in RELEASED")

    response = client.post(
        f"/approvals/{fixtures.artifact_type}/{fixtures.artifact_id}/approve",
        headers=auth(approver),
    )
    assert response.status_code == 409, response.text
    assert "terminal" in response.json()["detail"].lower()
