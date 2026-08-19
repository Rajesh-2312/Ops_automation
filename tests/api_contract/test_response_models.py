"""Does the JSON that comes back actually satisfy the declared `response_model`?

FastAPI validates outbound bodies against `response_model`, so in principle this
cannot fail. In practice `response_model` is only declared on some routes — the
two `.xlsx` endpoints declare `response_class=Response` and return bytes — and a
route whose return annotation and decorator disagree is exactly the sort of thing
nobody notices until a client parses it.

So this walks the real OpenAPI document, calls each GET route that needs no
constructed body, and validates the response against the schema the document
promises. It is the machine-checkable half of "does the response shape match the
Pydantic model"; the hand-written half is `test_frontend_contract.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.api_contract.conftest import JULY, Fixtures, auth

pytestmark = pytest.mark.contract


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        schema = root["components"]["schemas"][name]
    return schema


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> list[str]:
    """A small structural check: required keys, null-ability, array-ness, scalar type.

    Deliberately not a full JSON Schema implementation. The failures worth
    catching here are the ones a hand-written TypeScript model would trip over —
    a missing field, an unexpected null, an object where an array was promised —
    and those are all visible at this depth.
    """
    problems: list[str] = []
    schema = _resolve(schema, root)

    if "anyOf" in schema:
        branches = [_resolve(b, root) for b in schema["anyOf"]]
        if any(b.get("type") == "null" for b in branches) and value is None:
            return problems
        non_null = [b for b in branches if b.get("type") != "null"]
        if not non_null:
            return problems
        # Accept if ANY non-null branch validates.
        for branch in non_null:
            if not _validate(value, branch, root, path):
                return problems
        return [f"{path}: matched no branch of anyOf (value={type(value).__name__})"]

    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not in enum {schema['enum']}"]
    if "enum" in schema:
        return problems

    kind = schema.get("type")
    if kind == "null":
        if value is not None:
            problems.append(f"{path}: expected null, got {type(value).__name__}")
    elif kind == "object":
        if not isinstance(value, dict):
            problems.append(f"{path}: expected object, got {type(value).__name__}")
            return problems
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}.{key}: required by the schema, absent from the response")
        for key, sub in props.items():
            if key in value:
                problems.extend(_validate(value[key], sub, root, f"{path}.{key}"))
    elif kind == "array":
        if not isinstance(value, list):
            problems.append(f"{path}: expected array, got {type(value).__name__}")
            return problems
        items = schema.get("items")
        for index, entry in enumerate(value[:5] if items else []):
            problems.extend(_validate(entry, items, root, f"{path}[{index}]"))
    elif kind == "string":
        if not isinstance(value, str):
            problems.append(f"{path}: expected string, got {type(value).__name__} ({value!r})")
    elif kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{path}: expected integer, got {type(value).__name__}")
    elif kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(f"{path}: expected number, got {type(value).__name__}")
    elif kind == "boolean" and not isinstance(value, bool):
        problems.append(f"{path}: expected boolean, got {type(value).__name__}")
    return problems


def _cases(fixtures: Fixtures) -> list[tuple[str, str, str, Any, str]]:
    """(label, method, path, body, openapi_path) for routes with a 2xx JSON model."""
    payout = {
        "deployment_id": fixtures.deployment_65k,
        "period_start": JULY[0],
        "period_end": JULY[1],
    }
    period = {"period_start": JULY[0], "period_end": JULY[1]}
    return [
        ("health", "GET", "/health", None, "/health"),
        ("copilot.corpora", "GET", "/copilot/corpora", None, "/copilot/corpora"),
        ("monitoring.rules", "GET", "/monitoring/rules", None, "/monitoring/rules"),
        ("monitoring.alerts", "GET", "/monitoring/alerts", None, "/monitoring/alerts"),
        ("payouts.queue", "GET", "/payouts?month=2026-07", None, "/payouts"),
        ("payouts.preview", "POST", "/payouts/preview", payout, "/payouts/preview"),
        ("payouts.validate", "POST", "/payouts/validate", payout, "/payouts/validate"),
        (
            "comms.list",
            "GET",
            f"/comms/messages?program_id={fixtures.program_id}",
            None,
            "/comms/messages",
        ),
        ("erm.list", "GET", "/erm/tasks", None, "/erm/tasks"),
        (
            "reports.governance",
            "POST",
            f"/reports/programs/{fixtures.program_id}/governance",
            period,
            "/reports/programs/{program_id}/governance",
        ),
        (
            "reports.governance.commercial",
            "POST",
            f"/reports/programs/{fixtures.program_id}/governance",
            {**period, "include_trainer_cost": True},
            "/reports/programs/{program_id}/governance",
        ),
        (
            "reports.feedback",
            "GET",
            f"/reports/programs/{fixtures.program_id}/feedback"
            f"?period_start={JULY[0]}&period_end={JULY[1]}",
            None,
            "/reports/programs/{program_id}/feedback",
        ),
        (
            "reports.college.summary",
            "GET",
            f"/reports/colleges/{fixtures.college_id}/summary"
            f"?period_start={JULY[0]}&period_end={JULY[1]}",
            None,
            "/reports/colleges/{college_id}/summary",
        ),
    ]


def test_every_readable_route_matches_its_declared_response_model(
    client, fixtures: Fixtures, openapi
) -> None:
    failures: list[str] = []
    for label, method, path, body, spec_path in _cases(fixtures):
        kwargs = {"json": body} if body is not None else {}
        headers = {} if path == "/health" else auth(fixtures.manager_id)
        response = client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 500:
            failures.append(f"{label}: HTTP {response.status_code} {response.text[:200]}")
            continue
        if response.status_code >= 300:
            failures.append(f"{label}: HTTP {response.status_code} {response.text[:200]}")
            continue

        operation = openapi["paths"][spec_path][method.lower()]
        code = str(response.status_code)
        content = operation["responses"].get(code, {}).get("content", {})
        schema = content.get("application/json", {}).get("schema")
        if schema is None:
            failures.append(f"{label}: no application/json schema declared for {code}")
            continue
        failures.extend(_validate(response.json(), schema, openapi, label))

    assert not failures, "response bodies that diverge from their model:\n" + "\n".join(failures)


def test_the_xlsx_routes_return_a_workbook_and_the_r4_headers(client, fixtures: Fixtures) -> None:
    """Both sheet routes declare `response_class=Response` and carry the verdict.

    The headers are not decoration: `payouts.ts` reads `X-Artifact-State` and
    `X-Validation-Blocked` off the response because the sheet in the user's
    Downloads folder is the one the headers describe, and treats an absent
    blocked header as blocked. If the server ever stops sending them, every
    downloaded sheet silently renders as unblocked.
    """
    body = {
        "deployment_id": fixtures.deployment_65k,
        "period_start": JULY[0],
        "period_end": JULY[1],
    }
    for path, kind in (
        ("/payouts/remuneration-sheet.xlsx", "remuneration"),
        ("/payouts/invoice-sheet.xlsx", "invoice"),
    ):
        response = client.post(path, json=body, headers=auth(fixtures.manager_id))
        assert response.status_code == 200, f"{path}: {response.text[:200]}"
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml"
        )
        # A real zip container, not an error page with the wrong content type.
        assert response.content[:2] == b"PK", f"{path} did not return a workbook"

        assert response.headers["X-Artifact-State"] == "DRAFT"
        assert response.headers["X-Validation-Blocked"] in ("true", "false")
        assert "X-Validation-Blocking-Codes" in response.headers

        disposition = response.headers["content-disposition"]
        assert "attachment" in disposition
        assert "_DRAFT.xlsx" in disposition, disposition
        assert kind in disposition


def test_the_openapi_document_is_servable_and_complete(client) -> None:
    """Docs are on outside prod, and the schema is what every client is generated from."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "byteXL Ops Intelligence Platform"
    assert sum(len(ops) for ops in spec["paths"].values()) == 36
