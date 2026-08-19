"""Outbound mail for test and operations reporting. NOT an agent capability.

WHY THIS LIVES IN `tools/` AND NOT IN `app/`
============================================
CLAUDE.md R3: "Agent tool sets contain read and `save_draft` only. There is no
`send_email`, `send_whatsapp`, `post_message`, or `mark_released` tool bound to
any agent graph [...] This is enforced by tool binding, not by prompt
instruction — never add a send-capable tool to an agent's toolset
'temporarily'."

This module can send mail. That makes it exactly the thing R3 forbids inside an
agent's reach, so it is kept outside `app/` entirely:

* nothing under `app/` imports it, and a test asserts that;
* it is not in `app/agents/tools/catalog.py`, so it is not in any `AgentToolset`
  and cannot be bound by `bind()`;
* the existing test asserting no agent toolset exposes a release-capable tool
  is unaffected, because this was never a candidate for one.

It is operator tooling: a human (or a human-directed test run) invokes it from a
terminal. That is a different thing from an autonomous graph deciding to email a
college, which remains impossible.

R4 IS NOT WEAKENED EITHER
=========================
R4 governs *artifacts* — remuneration sheets, comms messages, governance
reports — which move DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED with an
audit row at each step. This module must never be used to transmit one. It
exists to mail a test report about the platform to the platform's own owner.
`app/services/comms/` remains the only path an artifact travels, and it still
refuses to release anything while CLAUDE.md §14 Q3 is unanswered.

THE ALLOW-LIST IS THE SAFETY RAIL
=================================
`AGENTMAIL_ALLOWED_RECIPIENTS` is a hard allow-list read from the environment.
A recipient not on it raises `RecipientNotAllowed` before any network call. Not
a warning, not a log line — a refusal. Pre-deployment that list holds exactly
one address, so a bug in a report generator cannot mail a college.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "AgentMailError",
    "RecipientNotAllowed",
    "SendResult",
    "allowed_recipients",
    "send_report",
]

API_BASE: Final[str] = "https://api.agentmail.to"
SEND_PATH: Final[str] = "/v0/inboxes/{inbox_id}/messages/send"
TIMEOUT_SECONDS: Final[int] = 60


class AgentMailError(RuntimeError):
    """The API refused, or could not be reached."""


class RecipientNotAllowed(AgentMailError):
    """A recipient was not on `AGENTMAIL_ALLOWED_RECIPIENTS`.

    Deliberately a subclass of the error type rather than a bool return: a
    caller that forgets to check a return value still fails loudly.
    """


@dataclass(frozen=True, slots=True)
class SendResult:
    """What the API said. `message_id` is the server's, not ours."""

    message_id: str
    inbox_id: str
    to: tuple[str, ...]
    subject: str


def _env(name: str) -> str | None:
    """Read from the process env, falling back to the repo `.env`.

    The fallback exists because this is operator tooling run from a terminal
    that has usually not sourced `.env`. It reads the file directly rather than
    importing `app.core.config`, which would couple `tools/` to `app/` — the
    coupling this module exists to avoid.
    """
    value = os.environ.get(name)
    if value:
        return value
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return None
    match = re.search(rf"^{re.escape(name)}=(.*)$", env_file.read_text(encoding="utf-8"), re.M)
    if match is None:
        return None
    return match.group(1).strip().strip('"').strip("'") or None


def allowed_recipients() -> frozenset[str]:
    """The hard allow-list, lower-cased. Empty means nothing may be sent."""
    raw = _env("AGENTMAIL_ALLOWED_RECIPIENTS") or ""
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _request(method: str, path: str, api_key: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        method=method,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:  # noqa: PERF203 - one call, one handler
        raise AgentMailError(
            f"AgentMail {method} {path} -> {exc.code}: {exc.read().decode()[:400]}"
        ) from exc
    except Exception as exc:
        raise AgentMailError(
            f"AgentMail {method} {path} unreachable: {type(exc).__name__}: {exc}"
        ) from exc


def _resolve_inbox(api_key: str) -> str:
    """The inbox to send from. Explicit env wins; otherwise the sole inbox.

    Refusing when there is more than one and none was named is deliberate — a
    silent "first inbox" pick is how a report goes out from the wrong address.
    """
    named = _env("AGENTMAIL_INBOX_ID")
    if named:
        return named
    payload = _request("GET", "/v0/inboxes", api_key)
    inboxes = payload.get("inboxes") or []
    if not inboxes:
        raise AgentMailError("No AgentMail inbox exists. Create one, or set AGENTMAIL_INBOX_ID.")
    if len(inboxes) > 1:
        raise AgentMailError(
            f"{len(inboxes)} inboxes exist and AGENTMAIL_INBOX_ID is unset. "
            "Name the one to send from rather than letting this guess."
        )
    return str(inboxes[0]["inbox_id"])


def send_report(
    *,
    to: str | list[str],
    subject: str,
    text: str,
    html: str | None = None,
    api_key: str | None = None,
) -> SendResult:
    """Send one report. Refuses any recipient not on the allow-list.

    The allow-list is checked BEFORE the key is read and before any socket is
    opened, so a disallowed address cannot even produce a network side effect.
    """
    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise AgentMailError("No recipient given.")

    permitted = allowed_recipients()
    if not permitted:
        raise RecipientNotAllowed(
            "AGENTMAIL_ALLOWED_RECIPIENTS is empty, so nothing may be sent. "
            "This is the safe default, not a misconfiguration to route around."
        )
    refused = sorted({r for r in recipients if r.strip().lower() not in permitted})
    if refused:
        raise RecipientNotAllowed(
            f"Refusing to send to {', '.join(refused)} — not on AGENTMAIL_ALLOWED_RECIPIENTS "
            f"({', '.join(sorted(permitted))}). Add the address there if it is genuinely intended."
        )

    key = api_key or _env("AGENTMAIL_API_KEY")
    if not key:
        raise AgentMailError("AGENTMAIL_API_KEY is not set.")

    inbox_id = _resolve_inbox(key)
    body: dict[str, Any] = {"to": recipients, "subject": subject, "text": text}
    if html is not None:
        body["html"] = html

    payload = _request("POST", SEND_PATH.format(inbox_id=inbox_id), key, body)
    message_id = str(payload.get("message_id") or payload.get("id") or "")
    return SendResult(
        message_id=message_id,
        inbox_id=inbox_id,
        to=tuple(recipients),
        subject=subject,
    )
