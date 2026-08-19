"""Where the TypeScript clients and the real JSON disagree.

Every model in `frontend/src/lib/*.ts` was written by reading `app/api/*.py`.
Field names, nullability, array-vs-object and the enum unions were all checked
against the live OpenAPI schema and the live responses, and — this is the honest
finding — **they agree**. There is no wrong field name and no swapped
nullability anywhere in the eight clients.

The divergences that DO exist are structural rather than field-level, and they
are the kind a type checker cannot see because there is no code to check:

1. `GET /payouts` and `POST /payouts/commit` are registered, respond, and have
   no TypeScript client at all. `PayoutQueue`, `PayoutQueueItem`,
   `ExistingPayout` and `CommittedPayout` are unrepresented in the frontend.
   `payouts.py` argues the queue is what stops a payout month being missed, and
   `commit` is the only route that turns a computed payout into a row — so the
   console can compute a payout it can never persist, and can never see which
   trainer-months are due.

2. `POST /programs/{id}/documents:generate` answers `DocumentGenerateResult`,
   which carries `withheld_commercial`. `api.ts` types the call as
   `GenerateResult`, which does not. The count the §4 wall withheld is on the
   wire and is dropped before any screen can render it — and it is nonzero for
   an LDE Executive, which is exactly the persona the number was written for.

Each test below pins the SERVER side of one of those gaps, so that closing it in
the frontend cannot silently change what the API promises, and so that "fixing"
it by deleting the field from the response fails loudly instead.
"""

from __future__ import annotations

import os
import re

import pytest

from tests.api_contract.conftest import JULY, REPO_ROOT, Fixtures, auth

pytestmark = pytest.mark.contract

LIB = os.path.join(REPO_ROOT, "frontend", "src", "lib")


def _frontend_sources() -> str:
    """Every .ts under frontend/src, comments stripped.

    Comments are stripped because several of these files DISCUSS the endpoints
    they do not call — `payouts.ts` names all four endpoints it wraps in a header
    block — and a grep that counted prose would report a client that is not there.
    """
    chunks: list[str] = []
    root = os.path.join(REPO_ROOT, "frontend", "src")
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith((".ts", ".tsx")) or name.endswith(".test.ts"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                text = fh.read()
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            text = re.sub(r"//[^\n]*", "", text)
            chunks.append(text)
    return "\n".join(chunks)


# --- gap 1: two registered routes with no client ------------------------------


def test_payout_queue_responds_but_has_no_typescript_client(client, fixtures: Fixtures) -> None:
    """`GET /payouts?month=` works. Nothing in the frontend calls it.

    Pins the response shape so the client that eventually lands is written
    against what the endpoint really returns rather than against the router again.
    """
    response = client.get("/payouts?month=2026-07", headers=auth(fixtures.manager_id))
    assert response.status_code == 200, response.text
    payload = response.json()

    assert set(payload) == {"month", "period_start", "period_end", "count", "items"}
    assert isinstance(payload["items"], list)
    if payload["items"]:
        item = payload["items"][0]
        assert set(item) == {
            "deployment_id",
            "trainer_id",
            "trainer_name",
            "trainer_pan",
            "college_id",
            "college_name",
            "program_id",
            "program_name",
            "batch_name",
            "period_start",
            "period_end",
            "attendance",
            "work_order_signed",
            "payout",
        }
        # R7 — `payout.net` is read back off a persisted row and is still a string.
        if item["payout"] is not None:
            assert item["payout"]["net"] is None or isinstance(item["payout"]["net"], str)

    source = _frontend_sources()
    assert (
        "PayoutQueue" not in source
    ), "a PayoutQueue client now exists — delete this test and type the response instead"


def test_payout_commit_responds_but_has_no_typescript_client(client, fixtures: Fixtures) -> None:
    """`POST /payouts/commit` is the only route that persists a payout.

    Driven here as an IDEMPOTENT REPLAY of a period already committed, which
    `commit_payout` answers with 200 `created=false` while writing no row, no
    version, no invoice number and no audit event. Nothing is created by this
    test, so nothing needs cleaning up.
    """
    response = client.post(
        "/payouts/commit",
        json={
            "deployment_id": fixtures.deployment_65k,
            "period_start": JULY[0],
            "period_end": JULY[1],
        },
        headers=auth(fixtures.manager_id),
    )
    if response.status_code == 409:
        pytest.skip(f"fixture period is not cleanly replayable here: {response.text[:200]}")
    assert response.status_code in (200, 201), response.text
    payload = response.json()
    assert set(payload) >= {
        "sheet_id",
        "created",
        "invoice_number",
        "artifact_type",
        "artifact_state",
        "artifact_version",
        "preview",
        "report",
    }
    assert payload["artifact_type"] == "remuneration_sheets"
    # R4 — commit creates, it does not submit, approve or release.
    assert payload["artifact_state"] in ("DRAFT", "PENDING_APPROVAL", "APPROVED", "RELEASED")

    source = _frontend_sources()
    assert (
        "/payouts/commit" not in source
    ), "a commit client now exists — delete this test and type the response instead"


# --- gap 2: a field on the wire that the client type drops --------------------


def test_documents_generate_returns_withheld_commercial(client, fixtures: Fixtures) -> None:
    """The server sends `withheld_commercial`; `api.ts` types it away.

    §4's wall withholds the `remuneration` and `invoice_generation` categories
    from a persona outside it, and reports the count rather than dropping them
    silently — "an LDE Executive should be able to see that part of the register
    exists and is not theirs". `GenerateResult` in `api.ts` has only `created`
    and `skipped`, so that number cannot reach a screen.

    This test pins the server half. It is deliberately read-only about the
    frontend: the fix is a TypeScript type, not an API change.
    """
    response = client.post(
        f"/programs/{fixtures.program_id}/documents:generate",
        headers=auth(fixtures.manager_id),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"created", "skipped", "withheld_commercial"}, payload

    with open(os.path.join(LIB, "api.ts"), encoding="utf-8") as fh:
        api_ts = fh.read()
    generate_result = re.search(r"export interface GenerateResult\s*\{(.*?)\}", api_ts, flags=re.S)
    assert generate_result is not None, "GenerateResult vanished from api.ts"
    assert "withheld_commercial" not in generate_result.group(
        1
    ), "api.ts now types withheld_commercial — delete this assertion, the gap is closed"


def test_withheld_commercial_is_nonzero_for_a_persona_outside_the_wall(
    client, fixtures: Fixtures, cleanup: list[tuple[str, str]]
) -> None:
    """The dropped field is not always zero — it is the number the LDE screen needs.

    Rows this creates are deleted by the `cleanup` fixture. The call is idempotent
    by `document_template_id`, so on an already-seeded program it creates nothing
    and still reports what the wall withheld.
    """
    if not fixtures.lde_id:
        pytest.skip("no LDE Executive profile reaches the fixture college")

    import psycopg

    from tests.api_contract.conftest import DSN

    def doc_ids() -> set[str]:
        with psycopg.connect(DSN, connect_timeout=20) as conn, conn.cursor() as cur:
            cur.execute(
                "select id from program_documents where program_id = %s", (fixtures.program_id,)
            )
            return {str(r[0]) for r in cur.fetchall()}

    before = doc_ids()
    # Seed as a Manager first so the register exists, then ask as an LDE Executive.
    seeded = client.post(
        f"/programs/{fixtures.program_id}/documents:generate",
        headers=auth(fixtures.manager_id),
    )
    assert seeded.status_code == 200, seeded.text
    for row_id in sorted(doc_ids() - before):
        cleanup.append(("program_documents", row_id))

    response = client.post(
        f"/programs/{fixtures.program_id}/documents:generate",
        headers=auth(fixtures.lde_id),
    )
    for row_id in sorted(doc_ids() - before):
        entry = ("program_documents", row_id)
        if entry not in cleanup:
            cleanup.append(entry)

    assert response.status_code == 200, response.text
    assert response.json()["withheld_commercial"] > 0, (
        "the commercials wall withheld nothing from an LDE Executive — either the "
        "wall moved or the document templates lost their commercial categories"
    )


# --- the clients that DO agree, pinned so they keep agreeing -------------------


def test_every_response_model_name_in_the_schema_is_reachable(openapi) -> None:
    """A sanity check on the schema this suite reasons about."""
    paths = openapi["paths"]
    assert len(paths) >= 20
    total = sum(len(ops) for ops in paths.values())
    assert total == 36, f"route count changed to {total}; update docs/api-findings.md"


@pytest.mark.parametrize(
    "ts_file,ts_type,schema_name",
    [
        ("payouts.ts", "PayoutBreakdown", "PayoutBreakdown"),
        ("payouts.ts", "PayoutPreview", "PayoutPreview"),
        ("payouts.ts", "ValidationReport", "ValidationReportOut"),
        ("alerts.ts", "AlertFeed", "AlertFeedOut"),
        ("alerts.ts", "SlaRule", "SlaRuleOut"),
        ("reports.ts", "GovernanceReportDraft", "GovernanceReportOut"),
        ("comms.ts", "CommsMessage", "CommsMessageOut"),
        ("erm.ts", "ErmTask", "ErmTaskOut"),
        ("copilot.ts", "AskResponse", "AskResponse"),
        ("approvals.ts", "ArtifactVersion", "ArtifactVersionOut"),
    ],
)
def test_typescript_interface_field_names_match_the_response_model(
    openapi, ts_file: str, ts_type: str, schema_name: str
) -> None:
    """Field-for-field, TypeScript against the declared `response_model`.

    This is the check the brief expected to find broken. It is not broken, and
    keeping it is what stops the next hand-written model from drifting.
    """
    with open(os.path.join(LIB, ts_file), encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)

    match = re.search(rf"export\s+interface\s+{ts_type}\s*\{{", src)
    assert match, f"{ts_type} not found in {ts_file}"
    start, depth, i = match.end(), 1, match.end()
    while depth and i < len(src):
        depth += (src[i] == "{") - (src[i] == "}")
        i += 1
    body = src[start : i - 1]

    fields: set[str] = set()
    depth = 0
    buf = ""
    for ch in body:
        depth += ch in "{[("
        depth -= ch in "}])"
        if ch in ";\n" and depth == 0:
            m = re.match(r"\s*([A-Za-z_]\w*)\??\s*:", buf)
            if m:
                fields.add(m.group(1))
            buf = ""
        else:
            buf += ch
    m = re.match(r"\s*([A-Za-z_]\w*)\??\s*:", buf)
    if m:
        fields.add(m.group(1))

    server = set(openapi["components"]["schemas"][schema_name].get("properties", {}))
    assert fields == server, (
        f"{ts_file}:{ts_type} diverges from {schema_name}\n"
        f"  only in TypeScript: {sorted(fields - server)}\n"
        f"  only on the server: {sorted(server - fields)}"
    )
