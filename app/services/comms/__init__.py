"""The Comms Service — CLAUDE.md §8, "Shared services (not agents)":

    "Comms Service — single outbound queue. Channel, recipient, template, and
     diff-from-template shown at approval."

One queue, `public.comms_messages` (migration 1700), that every outbound message
passes through before it is eligible to leave. Four modules, all pure:

    types      the two vocabularies — channel, recipient class
    diff       render a template to a BASELINE, then diff the message against it
    authority  who may approve. Deliberately empty: CLAUDE.md §14 Q3 is open
    lifecycle  the R4 ladder, as an ADAPTER over app/services/approval/

Nothing here opens a connection or sends anything. Persistence and the
authenticated-human endpoints are `app/api/comms.py`.

THREE PROPERTIES WORTH KNOWING BEFORE READING THE CODE
======================================================
**Releasing does not transmit.** No provider is wired — no SMTP, no Twilio, no
SendGrid, no outbound HTTP of any kind — and none may be added here. RELEASED
means an authenticated human cleared the message to go, which makes it *eligible*
to be sent by a transport built later. There is deliberately no `sent_at` column.

**Nothing can be approved today, and that is correct.** §14 Q3 — "Approval
authority for college-facing comms: Manager or Senior Manager?" — is unanswered,
so `authority.COMMS_APPROVAL_AUTHORITY` is empty and every approve, reject and
release raises, surfacing as 501 with the question in the body. Drafting,
amending and submitting work. The queue fills and stops. §14 says carry the open
questions and do not invent answers; a plausible default here would convert a
governance question into a silent answer nobody revisits.

**The lifecycle is borrowed, not rebuilt.** `lifecycle.py` calls
`app.services.approval.check_transition()` for the grammar and
`app.services.approval.content_hash()` for the freeze, and raises the shared
error types. The only thing it owns is the comms-specific authority lookup and
the payload that gets hashed. See its docstring for why `state_machine.Artifact`
itself could not be used and what would have to change for it to be.
"""

from __future__ import annotations

from app.services.comms.authority import (
    COMMS_APPROVAL_AUTHORITY,
    CommsApprovalAuthorityUndefinedError,
    CommsUnauthorisedApproverError,
    comms_approver_personas,
)
from app.services.comms.diff import (
    DIFF_VERSION,
    DiffOp,
    Hunk,
    TemplateDiff,
    TemplateRenderError,
    UnrenderableValueError,
    diff_from_template,
    render,
)
from app.services.comms.lifecycle import (
    COMMS_ENTITY_TABLE,
    CommsAction,
    CommsOutcome,
    QueuedMessage,
    amend_message,
    approve_message,
    comms_payload,
    queue_message,
    reject_message,
    release_message,
    submit_message,
    supersede_message,
    verify_message_frozen,
)
from app.services.comms.types import CommsChannel, CommsRecipientKind

__all__ = [
    "COMMS_APPROVAL_AUTHORITY",
    "COMMS_ENTITY_TABLE",
    "DIFF_VERSION",
    "CommsAction",
    "CommsApprovalAuthorityUndefinedError",
    "CommsChannel",
    "CommsOutcome",
    "CommsRecipientKind",
    "CommsUnauthorisedApproverError",
    "DiffOp",
    "Hunk",
    "QueuedMessage",
    "TemplateDiff",
    "TemplateRenderError",
    "UnrenderableValueError",
    "amend_message",
    "approve_message",
    "comms_approver_personas",
    "comms_payload",
    "diff_from_template",
    "queue_message",
    "reject_message",
    "release_message",
    "render",
    "submit_message",
    "supersede_message",
    "verify_message_frozen",
]
