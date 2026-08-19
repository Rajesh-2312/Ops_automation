# Attendance and payable days

## What this document covers and who it is for

Trainer attendance is the day-by-day record of a trainer's work on a deployment.
It is the only input that decides how many days a trainer is paid for, so a mark
that is wrong or missing is not a clerical detail — it is the whole dispute. This
document is written for the LDE Executive who marks the days and for the Manager
who has to defend the count a month later.

The counting rules differ between the two program types, and the difference runs
in opposite directions. The same missing mark that quietly costs a CRT trainer a
day's work quietly grants a bCAP trainer one. Everything below is about counting
days. The arithmetic that turns a day count into money is a separate document,
and it is walled from campus staff.

## Who marks trainer attendance, and why the trainer does not

Attendance is marked by internal byteXL staff whose reach covers the deployment.
In practice that is the LDE Executive on the campus; the Manager and Senior
Manager who cover the same college can mark and correct days as well. The
database enforces this: the policy on the trainer attendance table requires an
internal persona and a reach test on the deployment, so a member of staff cannot
mark a day at a college they do not hold.

Trainers do not mark their own attendance, and cannot. Educators do not sign in
to this platform at all — they are records managed on their behalf by internal
staff, which was the owner's decision of 18 August 2026. The migration that
implemented it removed every policy the trainer persona held, four of which were
writes. Two of those writes let the person being paid mark the days that decided
their own pay.

That arrangement had an argument behind it while trainers were users of the
system: they could insert a mark but never update or delete one, so a mistaken
mark failed loudly against the uniqueness rule rather than being silently
overwritten. The argument was sound and is now moot. Marking attendance is wholly
the campus team's job, and corrections go through the person who holds the
campus, which is who the trainer would be talking to anyway.

## One row per day, and why a wide D1 to D31 sheet is not allowed

Attendance is stored as one row per trainer-day against a deployment, with a
uniqueness rule on the pair of deployment and date. A second row for the same day
is impossible rather than discouraged.

This is not a storage preference. Disputes are always about specific days, and a
wide layout with one column per day of the month cannot answer "who changed the
fourteenth, and when". Each row carries the day, the mark, who marked it, when it
was marked, and a free-text note. The note is where the reason for a holiday or a
half day goes; it is read by humans resolving a disagreement and is never parsed
by the system.

Two rows for one day would also let two marks disagree with no tie-breaker while
the counting code silently picked one. The uniqueness rule is what makes that
impossible, and the counting code additionally rejects a duplicated day outright,
because a duplicate would double-count on both program paths.

## The five marks and what each one means

A day carries one of five marks. `P` is present. `A` is absent. `H` is a half
day. `HOLIDAY` is a college holiday. `UNMARKED` is a day nobody has yet recorded
anything about.

A day with no row at all and a day explicitly marked `UNMARKED` are the same
thing to the counting code. Before counting, the period is expanded to every
calendar day between the start and end dates inclusive, and any day without a
stored mark is filled in as `UNMARKED`. That materialisation matters on the bCAP
path, where the count begins at the length of the period: handing the counter
only the days someone happened to touch would shorten the period itself and
under-count a retainer.

A mark dated outside the deployment's own window is not rejected by the database.
That check was deliberately left to validation in the application layer, where
the failure can name the day and the window rather than surfacing as an opaque
constraint violation while somebody is typing.

## How payable days are counted on a CRT program: counting up

A CRT engagement is paid per day worked, so the count starts at zero and adds
evidence. A `P` adds a full day. An `H` adds half a day. An `A` adds nothing. A
college holiday adds nothing, because it is not a worked day. An `UNMARKED` day
adds nothing, because there is no evidence that anything was worked.

Weekends need no special rule and deliberately have none. Nobody marks `P` on a
Saturday, so a weekend contributes nothing simply by not being worked. Encoding a
weekday rule instead would be wrong for the batches that genuinely do run on
Saturdays, and it would be wrong invisibly.

## How payable days are counted on a bCAP program: counting down

A bCAP engagement is a monthly retainer prorated across calendar days, so the
count starts at the full length of the period and subtracts absence. An `A`
subtracts a full day. An `H` subtracts half a day. A `P` subtracts nothing, a
college holiday subtracts nothing, and an `UNMARKED` day subtracts nothing.

Weekends and college holidays are payable on bCAP. The retainer absorbs them, and
deducting for a college holiday would charge the trainer for the college's own
calendar. The count is floored at zero, so a period of absences plus a data-entry
error produces "nothing owed" rather than a negative day count that would invert
the sign of everything downstream.

## Why an unmarked day is dangerous in opposite directions

`UNMARKED` contributes zero on both paths, and that symmetry is a trap. On CRT
the zero means "no evidence of work, pay nothing". On bCAP the zero means
"nothing deducted, the retainer stands". The same absent mark therefore
under-counts a CRT trainer and over-counts a bCAP one, and neither failure raises
an error anywhere. Both just produce a number.

This is why the counting code writes the two paths out separately instead of
sharing one table of per-mark weights. A shared table would be shorter and would
hide the asymmetry that is the single most consequential rule in the system.

The practical instruction for campus staff is short: mark every day of the
period, including weekends and holidays, on both program types. A `HOLIDAY` mark
is not paperwork. On CRT it is the difference between a day that is correctly
unpaid and a day that looks like an oversight; on bCAP it is the difference
between a documented payable day and a gap somebody will query.

## Why incomplete attendance blocks a CRT cycle and only warns on bCAP

Attendance completeness — every day of the period carrying a mark other than
`UNMARKED` — is checked before a cycle can be submitted for approval. It is a
blocking failure on CRT and a warning on bCAP, and the asymmetry follows directly
from the counting rules above.

On CRT, an unmarked day is an under-count nobody notices. The trainer is short of
a day, the arithmetic is internally consistent, and there is no error to
investigate. That failure mode has to be stopped before it ships, so it blocks.

On bCAP, an unmarked day is an over-count, and an over-count is something a human
can look at and consciously accept. So it warns, and the warning demands a stated
reason recorded against the run. Blocking bCAP instead would stall every retainer
cycle over a single missing weekend mark, and warning on CRT would ship silent
under-counts. Neither severity should ever be "simplified" into the other.

The block names how many days are unmarked rather than saying only "attendance
incomplete", because a completeness failure without a count sends somebody
hunting through a month of rows.

## Correcting a mark that is already recorded

Corrections are made by the internal staff who hold the campus. Because there can
only ever be one row per day, a correction is an edit to the existing day rather
than a second, competing mark, and the row keeps the timestamp of its last
change. Put the reason in the day's note: the note exists for exactly this, and a
disagreement resolved six weeks later is resolved by reading it.

Correct the day before the cycle is submitted for approval wherever possible. A
cycle that has been approved is frozen, and changing what it was computed from
means a new version that needs fresh approval.

## Student attendance is a different record and is not an input to pay

There are two attendance tables and they are not interchangeable. Student
attendance records who turned up to a session; it feeds the college-facing
summary and nothing else. Trainer attendance is one row per trainer-day and is
the input to the payable-day count.

They are separate because they disagree about almost everything: the grain is
per-student-session against per-trainer-day, the vocabulary is present, absent,
late and excused against `P`, `A`, `H`, `HOLIDAY` and `UNMARKED`, and the
security class is different — a student attendance percentage is an aggregate a
college may see, while a trainer's day record decides what a contractor is paid
and is never exposed to a college in any form. A mark of "late" has no meaning in
the payable-day count, and a half day has no meaning for a student.
