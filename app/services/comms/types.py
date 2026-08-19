"""The two vocabularies of the outbound queue: channel and recipient class.

CLAUDE.md §11 says "Enums in `domain/`, never string literals for status values",
and these two belong in `app/domain/enums.py`. They are here instead, and that is
a known departure with a reason rather than an oversight:

* `app/domain/enums.py` is closed to this workstream — see the note in
  `app/api/comms.py` on §14 Q3, which is the same boundary.
* `app/db/models.py` already carries six Postgres enums with no domain
  counterpart (`document_category`, `document_status`, and four others), spelled
  out inline via `_pg_enum()` and typed `str`. These two follow that established
  pattern exactly, and `1700_comms_queue.sql` says so at the point of declaration.
* `app.services.approval.state_machine.ApprovalAction` set the precedent for an
  enum living beside its only consumer when the shared file cannot be edited.

**When `app/domain/enums.py` opens, move both there verbatim and re-export from
here for one release.** The values are the Postgres labels, so moving the class
is a refactor; changing a value would be a migration.

`StrEnum`, not `(str, Enum)`, for the reason `app/domain/enums.py` gives at
length: on 3.11+ the latter formats as `"CommsChannel.EMAIL"` inside an f-string
while `==` keeps passing, so the wrong token reaches SQL silently. Being `str`
subclasses is also what lets a member bind straight into the `_pg_enum()` column
in `app/db/models.py` without that layer importing this one.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["CommsChannel", "CommsRecipientKind"]


class CommsChannel(StrEnum):
    """How a queued message would physically leave, once a provider exists.

    Three labels, not five. These are the channels CLAUDE.md actually names — R3
    forbids a `send_email`, `send_whatsapp` or `post_message` tool on any agent,
    and §8 gives the Onboarding agent "platform tickets". A fourth label would be
    an invitation to build the integration behind it before anyone asked.

    None of them transmits anything today. The channel is what an approver reads
    to judge the message ("this is going to a college on WhatsApp") and what a
    provider will one day route on.
    """

    EMAIL = "email"
    WHATSAPP = "whatsapp"
    PLATFORM_TICKET = "platform_ticket"


class CommsRecipientKind(StrEnum):
    """Who is on the other end, as a class rather than as an address.

    This is the field the CLAUDE.md §8 autonomy ladder reads. "Nothing touching
    money, contracts, or a college contact goes past level 3", while "internal
    chase messages and platform tickets may reach level 4 only after a
    demonstrated track record" — two different ceilings, and an email address on
    its own cannot tell them apart.

    `TRAINER` is deliberately its own class rather than folded into external: a
    trainer is a contracted counterparty, so a message to one is contract-adjacent
    and takes the level-3 ceiling, but it is not a college contact and the
    approval question about it is different.
    """

    INTERNAL_STAFF = "internal_staff"
    TRAINER = "trainer"
    COLLEGE = "college"
