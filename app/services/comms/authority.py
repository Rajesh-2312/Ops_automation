"""Who may approve an outbound message. CLAUDE.md §14 Q3, unanswered.

    3. Approval authority for college-facing comms: Manager or Senior Manager?

§14's instruction about the open questions is one sentence long — "Carry these;
do not invent answers" — and this module is what carrying it looks like in code.

`COMMS_APPROVAL_AUTHORITY` below is **deliberately empty**. Not empty because
nobody got to it: empty because the organisation has not decided, and the only
honest representations of an undecided question are "no entry" and a loud failure
at the moment somebody tries to act on it. `comms_approver_personas()` raises,
`app/api/comms.py` maps that to **501 Not Implemented** carrying the exception's
own message, and the person who wanted to approve a message reads the question
rather than hunting for a permission that does not exist.

The practical consequence is worth stating plainly rather than discovering: today
a comms message can be drafted, amended and submitted for approval, and **cannot
be approved, rejected or released by anybody**. The queue fills and stops. That is
the correct behaviour for a system whose rule R4 is "nothing leaves the system
unapproved" and which does not yet know who approves. Do not unblock it by
picking the permissive option.

WHY THIS TABLE IS HERE AND NOT IN `app/domain/enums.py`
======================================================
`APPROVAL_AUTHORITY` there is keyed by `ArtifactType`, a closed three-label enum
whose values are the table names `remuneration_sheets`, `governance_reports` and
`program_documents`. A comms message is none of those, and adding a fourth label
means editing that enum, its Postgres mirror `public.artifact_type`, and the
`artifact_versions` schema in one change — which `1700_comms_queue.sql` explains
at length and names as the migration that should accompany the ANSWER to Q3, not
precede it.

So the shape is duplicated and the decision is not: this dict is keyed by
`CommsRecipientKind` because that is the axis Q3 is actually about (a college
contact is the case in question; an internal chase is not), and it holds nothing.
When Q3 is answered, the right change is to move comms onto `artifact_versions`
and delete this file — see the migration header.

RELEASE AUTHORITY IS READ AS APPROVAL AUTHORITY, conservatively and for the same
reason `app.services.approval.state_machine` gives: R3 requires release to be an
authenticated human session and R4 keeps release distinct from approval, but no
persona is named for it anywhere in CLAUDE.md. Reusing the approval set can never
permit somebody who could not have approved; picking anything wider would be an
invention. Both actors are recorded separately so a four-eyes rule needs no data
migration.
"""

from __future__ import annotations

from typing import Final

from app.domain.enums import Persona
from app.services.approval import ApprovalError
from app.services.comms.types import CommsRecipientKind

__all__ = [
    "COMMS_APPROVAL_AUTHORITY",
    "CommsApprovalAuthorityUndefinedError",
    "CommsUnauthorisedApproverError",
    "comms_approver_personas",
]

#: Personas that may approve an outbound message, by recipient class.
#:
#: **Empty on purpose. Do not fill this in to make a test pass.** See the module
#: docstring: §14 Q3 is open, and a plausible default here would convert an open
#: governance question into a silent answer that nobody ever revisits.
#:
#: Typed rather than left implicit so that the day it is filled, the type says
#: what a correct entry looks like.
COMMS_APPROVAL_AUTHORITY: Final[dict[CommsRecipientKind, frozenset[Persona]]] = {}


class CommsApprovalAuthorityUndefinedError(ApprovalError):
    """No persona has been given authority to approve messages to this recipient.

    Subclasses `ApprovalError` — the base
    `app.services.approval.state_machine` uses — so a caller that already handles
    governance refusals from the artifact lifecycle handles this one too, and so
    a broad `except ValueError` around request parsing cannot swallow "a message
    left the system unapproved".

    Deliberately NOT `ApprovalAuthorityUndefinedError`: that one takes an
    `ArtifactType` and names an artifact table, and reporting a comms message as
    a `remuneration_sheets` problem would send the reader to the wrong file.
    """

    def __init__(self, recipient_kind: CommsRecipientKind) -> None:
        super().__init__(
            f"No approval authority is defined for a comms message to "
            f"'{recipient_kind.value}'. CLAUDE.md §14 Q3 — 'Approval authority for "
            "college-facing comms: Manager or Senior Manager?' — is an open question, and "
            "§14 says carry it, do not invent an answer. No outbound message can be "
            "approved, rejected or released until COMMS_APPROVAL_AUTHORITY in "
            "app/services/comms/authority.py records a decision made by a human. Drafting "
            "and submitting still work; the queue is meant to fill and stop (R4)."
        )
        self.recipient_kind = recipient_kind


class CommsUnauthorisedApproverError(ApprovalError):
    """The actor's persona is not in this recipient class's authority set.

    The comms twin of `app.services.approval.UnauthorisedApproverError`, which
    cannot be reused as-is because its constructor takes an `ArtifactType` and
    would report a comms message as a problem with one of the three artifact
    TABLES — sending whoever reads the 403 to the wrong file. Same base class, so
    `app/api/comms.py` maps both to the same status.

    **Unreachable while `COMMS_APPROVAL_AUTHORITY` is empty**, because
    `comms_approver_personas()` raises before any comparison happens. It is
    written anyway so that answering §14 Q3 is a one-line change to the dict
    rather than a design exercise under time pressure.
    """

    def __init__(
        self,
        recipient_kind: CommsRecipientKind,
        persona: Persona,
        permitted: frozenset[Persona],
    ) -> None:
        names = ", ".join(sorted(p.value for p in permitted))
        super().__init__(
            f"Persona '{persona.value}' may not approve, reject or release a comms "
            f"message to '{recipient_kind.value}' — permitted: {names}."
        )
        self.recipient_kind = recipient_kind
        self.persona = persona
        self.permitted = permitted


def comms_approver_personas(recipient_kind: CommsRecipientKind) -> frozenset[Persona]:
    """Personas that may approve, reject or release a message to this recipient.

    Raises `CommsApprovalAuthorityUndefinedError` for every recipient kind today.
    That is the whole point — see the module docstring. Mirrors
    `app.services.approval.state_machine.approver_personas()` exactly, including
    its rule that an entry present but EMPTY raises too: "nobody may approve
    this" is not a decision somebody made, it is an incomplete one, and it should
    fail the same way.
    """
    permitted = COMMS_APPROVAL_AUTHORITY.get(recipient_kind)
    if not permitted:
        raise CommsApprovalAuthorityUndefinedError(recipient_kind)
    return permitted
