# ERM sync and the field pack

## What this document covers and who it is for

ERM is an external system byteXL uses that this platform cannot talk to. This
document explains how records are kept aligned between the two anyway, what the
system asks a person to do, and what happens when a record changes after it has
been synced. It is written for whoever is handed an ERM sync task and for the
Manager who wants to know whether the two systems still agree.

## Why there is no ERM integration, and no scraper

ERM has no API. There is nothing to call, and there is deliberately no scraper.
Nothing in this platform sends anything to ERM, reads anything from it, or holds
credentials for it.

The reasoning is stated as policy rather than left as an implementation detail: a
feature is never blocked on API access that does not exist, and a scraper is a
dependency on somebody else's markup that breaks silently and at the worst
moment. The same pattern applies to any external system that lacks an interface —
model the human step honestly instead of pretending to automate it.

Access to the EdTech platform is an open question of the same shape. CLAUDE.md §14
question 6 asks whether that access is a direct database connection, an API, or
neither, and it is unanswered. Until it is answered, nothing should be designed
that assumes an answer.

## What a sync task actually is

A sync task is a job card for a named person. The system generates the exact list
of field-and-value pairs in ERM's field order, assigns it to somebody, that person
opens ERM and pastes them in, and then comes back and confirms that they did.
That is the whole integration.

The job card has a small life of its own: it is queued, then assigned, then
confirmed, and it can also go stale or be cancelled. The record it refers to
carries its own sync status — not pushed, pending, synced, stale, or failed —
along with when it was synced and who synced it.

What is captured on confirmation is taken from the trackers byteXL already keeps
for this: who pasted, when they pasted, the ERM identifier they read back off the
screen afterwards, whether anyone verified it, and free-text remarks. Those
columns exist because the existing update logs record exactly those things, which
is good evidence of what is worth capturing about a paste.

A confirmed sync task cannot be deleted. It is the evidence that a named person
transferred a named set of values on a named day, which is the record this design
is here to produce.

## What a field pack contains, and what it deliberately leaves out

A field pack is values from a row, rendered in order. Nothing in it is computed.
If a future field would need arithmetic, it does not belong in a pack — this is a
transcription problem and it stays one.

A pack carries no commercial values at all: no rate, no bank account, no IFSC, no
P&L line. This is not tidiness. Sync task rows are readable by campus staff on the
same terms the trainer record is, so a pack that carried a day rate would route a
commercial value straight around the wall that keeps such values away from an LDE
Executive. PAN appears in the trainer pack because it is the trainer's identity
key, not because it is financial. A test asserts the absence by scanning the
declared fields rather than trusting a comment.

A pack also carries no value that ERM itself owns. The ERM identifier is what ERM
tells us, read off its screen after the paste. Putting it into the pack would ask
somebody to type ERM's own identifier back into ERM, and it would make that field
a watched one, so recording the identifier would instantly mark the record stale.

## The field order is a guess, and every layer says so

The design asks for ERM's own field order. Nobody on this side of the integration
has seen ERM's form, and the legacy folders cannot supply it: the two ERM
artefacts on file are logs of the update rather than the update itself. They are
trackers with columns for the college, the program or trainer name, the date it
was updated, who updated it, the ERM identifier, whether it was verified, a status
and remarks. Those tell us what is worth recording about a paste. They say nothing
about the order of the fields on ERM's screen.

So the order is declared in exactly one place, marked unverified, and versioned.
The unverified flag travels through the API response and onto the screen, so
nobody can mistake the guess for a specification, and the version is stamped onto
every task row. When somebody finally opens ERM and writes the real order down,
they correct the declaration and bump the version, and every pack generated under
the guess stays identifiable as such.

Inventing an order and presenting it as ERM's would make the guess unfalsifiable,
which is worse than an order that announces itself as provisional.

## Drift: what makes a synced record go stale

An update that changes a field the pack carries, on a record that has already been
synced, flips that record to stale and queues a new sync task. That is drift
detection, and it is the half of this design that decides whether anyone trusts
either system a month from now. Without it the two diverge quietly and neither is
believed.

The watch list is the pack, not the table. A change to a field ERM does not
receive is not drift and does not requeue anything; a change to a name that the
pack does carry is drift. The list of watched columns is duplicated between the
database triggers and the code that builds the pack, and the duplication is
policed by a test that reads both and fails if they disagree. Adding a field to a
pack therefore forces the watch list to be updated too.

## Why confirming a sync does not itself look like drift

The confirmation writes the sync status, the timestamp, who did it, and the ERM
identifier. None of those is a watched field, so the act of recording a successful
sync cannot mark the record stale on its way in.

Getting this backwards produces a record that goes stale one millisecond after
every sync. That reads as "the detector is working" for about a week, right up
until everybody switches it off.

## Drift that arrives sideways

Drift arrives sideways as often as head-on, and this is the case the design is
really guarding against.

The trainer pack includes the college the trainer is assigned to, which is not a
column on the trainer record at all — it is derived by walking from the deployment
to the batch to the program to the college. Moving a trainer to another campus
therefore drifts their ERM record without anybody touching the trainer row. A
college being renamed does the same thing to the program pack. Both cases have
their own detection, because a detector that only watched the obvious table would
be silently wrong in precisely the two situations that actually happen.

## Why one edited record produces one job card and not five

Requeueing is idempotent. Only one open sync task can exist per subject at a time,
so a record that somebody edits five times in an afternoon produces one job card
rather than five. Somebody who has to work through a queue of duplicates stops
working through the queue.

## Why a sync task carries no approval step

Nothing here goes through the draft, approval and release lifecycle that governs
artifacts leaving the building. Pasting a trainer's contact details into a portal
byteXL already uses is internal record-keeping between two systems that hold the
same data, not an artifact reaching a college or a trainer. The onboarding work
around ERM is explicitly allowed to run automatically because it is internal only.

That is a decision with a condition attached: if ERM ever became college-facing,
this is the decision to revisit first.
