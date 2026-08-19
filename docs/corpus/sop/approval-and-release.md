# Approval and release

## What this document covers and who it is for

Nothing leaves this system unapproved. This document explains the four states
every artifact moves through, what approval actually does to a document, why
approval and release are two separate acts rather than one, and why an agent can
prepare a message but can never send it. It is written for anyone who approves
something — a payout, a report, a college-facing message — and for anyone who has
wondered why the button they expected is not there.

An artifact here means anything the platform produces that a human signs off:
today that is remuneration sheets, governance reports and program documents, with
outbound messages queued through the same discipline.

## The four states, and the only moves between them

An artifact is created in DRAFT. From DRAFT it can be submitted, which moves it to
PENDING_APPROVAL. From PENDING_APPROVAL it can be approved, which moves it to
APPROVED, or rejected, which returns it to DRAFT. From APPROVED it can be
released. RELEASED is terminal and nothing follows it.

That list is the entire grammar. Anything not in it is a defect rather than an
edge case, and an attempt to make an illegal move raises an error rather than
returning a "no". The distinction is deliberate: a returned boolean gets ignored
at exactly one call site eventually, and that call site is the one that releases
something nobody approved.

There is deliberately no edge from APPROVED back to DRAFT. Approval is not
reversible in place, because reversing it in place would destroy the record of
what was approved.

## What approval freezes, and what release checks

Approval freezes the version and takes a hash of it. The hash is computed over a
canonical form of the artifact's payload, so the same content produces the same
digest on any machine, in any process, months later, regardless of the order a
dictionary happened to be built in or whether a rupee value arrived with trailing
zeros.

Release recomputes that hash and refuses to proceed if the payload has moved since
approval. Without the recheck, "approved" would mean "was approved once, in a
state nobody can now reconstruct", which is not a statement anyone can defend in a
dispute.

The canonicalisation makes a few choices worth knowing about. Values are tagged
with their type, so the number six and the text "6" are different content — a
freeze that cannot tell those apart cannot tell a corrected sheet from an
original. Decimal values are rendered exactly, without normalising, because a hash
helper that alters the value it is fingerprinting is worse than no hash. Floating
point values are refused outright rather than converted, since converting would
launder a defect into a freeze that looks authoritative.

## Why approval and release are two separate actions

Approval and release are separate calls, produce separate audit rows, and require
separate human decisions. There is no combined approve-and-release operation and
there must never be one.

The reason is the second decision. Two actions mean there is a moment, after
somebody has approved the content, at which somebody can still decline to send it —
because the timing is wrong, because the recipient has changed, because the
college called this morning. Collapsing them removes that moment and makes it
impossible to answer the only question that matters afterwards: who approved this,
and did anyone actually send it.

The approver and the releaser are recorded separately, which also means a
requirement for two different people can be added later without a data migration.

## Editing an approved artifact creates a new version

Editing an approved artifact is not a transition and does not move it backwards.
It creates a new version, one higher, in DRAFT, with the freeze cleared, requiring
fresh approval on its own terms.

The approved version stays exactly as it was. That is what makes it possible to
answer, six weeks later, what the Senior Manager actually approved — as opposed to
what the document says now.

A rejection is different: it returns the artifact to DRAFT at the same version and
requires a stated reason. A rejection without a reason is refused, because
"rejected" with no explanation sends the drafter back to guess.

## Who may approve what, and what is deliberately undecided

A remuneration sheet is approved by a Senior Manager. That follows directly from
the persona definition, which places payout approval with that persona.

No other artifact type has an approval authority defined, and the absence is
deliberate rather than unfinished. CLAUDE.md §14 question 3 asks whether
college-facing communications are approved by a Manager or a Senior Manager, and
it is unanswered. Attempting to approve an artifact type with no authority raises
a loud, specific error naming the question, and the API returns a "not
implemented" response carrying that message.

The practical consequence is worth stating plainly rather than discovering. Today
an outbound message can be drafted, amended and submitted for approval, and cannot
be approved, rejected or released by anybody. The queue fills and stops. For a
system whose first rule is that nothing leaves unapproved, and which does not yet
know who approves, that is the correct behaviour — and the wrong way to unblock it
is to pick the more permissive option so that the queue moves.

Release authority is currently read as approval authority. That is a conservative
placeholder and is also open: the rules require release to be an authenticated
human session and keep release distinct from approval, but no persona is named for
it anywhere. Reusing the approval set can never permit somebody who could not have
approved in the first place, whereas picking anything wider would be an invention.
If a distinct releasing role is wanted — Finance and Accounts is an actor in the
process, not a persona — that is a second authority decision, not a widening of
this one.

## Approval requires a human, structurally

An approval, a rejection or a release attempted with no human behind it is
refused. A scheduled job or an agent may prepare an artifact and may submit it for
approval. Neither can approve it, and neither can release it.

Release endpoints require an authenticated human session. This is not a policy
that lives in a prompt; it is a check in the code path, and the person acting is
recorded on the audit row.

## Agents draft; they cannot send

Every agent's tool set contains reads and a single save-draft capability. There is
no tool anywhere in the system for sending an email, sending a message, posting to
a channel, or marking something released, and none is bound to any agent.

This is enforced by how tools are constructed rather than by instruction. Tools
are declared as pure data, in a structure with no field capable of holding a
function, and calls are routed through a closed table of read and draft
operations. A send-capable function is therefore not something an agent has been
asked not to use; it is something there is no way to attach. A test asserts that no
agent's tool set exposes anything release-capable, so if somebody adds one, the
build fails loudly rather than the capability appearing quietly.

The instruction that matters for anyone extending this: never add a send-capable
tool to an agent's tool set "temporarily".

## The autonomy ladder, and the ceiling on it

Agents sit on a four-rung ladder. The first rung is observing — reading,
reporting, raising an internal alert. The second is drafting, where the agent
proposes and a human edits and sends. The third is acting with approval, where a
human's single click executes something. The fourth is acting autonomously, with
everything logged.

Nothing that touches money, a contract, or a contact at a college goes past the
third rung. Internal chase messages and platform tickets may reach the fourth, and
only after a demonstrated track record — not on the strength of an argument that
they probably would have been fine.

Outbound communications all go through one queue rather than being sent from
wherever they were composed, and at the approval step the approver sees the
channel, the recipient, the template and the difference between the template and
what is actually about to go out. Approving a message you have not seen the final
text of is not approval.

## What the audit trail records, and why it cannot be edited

Every state transition writes an audit row: who acted, what they did, what the
thing looked like before, what it looks like after, and when. Approval and release
each write their own, which is what makes them separately answerable.

The audit table is append-only, and it is append-only several layers deep. There is
no policy permitting an update or a delete, for anybody, including administrators.
The grants for update, delete and insert are absent too, and a missing grant is
checked before any policy is. A trigger rejects updates, deletes and truncation
outright, because privileged connections walk past both policies and grants — and a
privileged connection is exactly what the service itself uses.

The cost of this is real and was accepted deliberately: a genuine correction to an
audit row is impossible without shipping a new migration that drops the guard,
under review, on the record. That is the intended level of friction. An audit trail
that can be edited is not an audit trail.
