# Trainer payout computation and validation gates

## What this document covers and who may read it

This document explains how a trainer's remuneration for a period is computed,
what has to be true before a payout cycle can be submitted for approval, and why
several rules that look fussy are the difference between a defensible payment and
an argument. It is written for the Manager who runs a monthly payout and for the
Senior Manager who approves it.

This is commercial content. Rates, remuneration, invoices and P&L are visible to
Senior Managers and Managers only; an LDE Executive is walled from all of it in
the database, not in the interface. If you are reading this and you work on
campus, the operational half — how attendance marks become a payable-day count —
is in the attendance document, which carries no commercial terms.

Nothing in this document is a figure. It contains no rate, no payout amount and
no trainer's details. Every actual number lives in the database and is read from
there.

## The order a payout is computed in, and why the order is fixed

A payout starts from two things: the payable-day count for the period, and the
engagement rate written in the signed work order. The rate basis decides the
branch. A CRT engagement is rated per day; a bCAP engagement is rated per month
and prorated across the calendar days of that month.

On the per-month branch, earned pay is the monthly rate multiplied by the payable
days and then divided by the days in the month. On the per-day branch there is no
divisor at all: earned pay is the day rate multiplied by the payable days.

Gross is earned pay plus the reimbursement columns — TA and DA, accommodation,
and travel reimbursement. TDS is charged on earned pay. Net is gross minus any
deductions and minus TDS, and the net is rounded to whole rupees. That rounding
is the only rounding in the entire chain.

The order is fixed because every step after the first consumes the step before
it. An implementation that reorders them is not a refactor of this policy; it is
a different policy, and it will disagree with this one in rupees.

## Why earned pay is multiplied before it is divided

On the per-month branch the multiplication comes first, deliberately and in a
single expression. Dividing the monthly remuneration by the days in the month
first leaves a repeating decimal in most months, and a repeating decimal does not
recombine: a full month of payable days comes back a hair under the monthly rate
rather than exactly equal to it.

This is not a theoretical concern. The legacy invoice spreadsheet carries exactly
that artifact today — a full month's earned pay, gross and net are all stored a
fraction of a paisa above the round rupee figure, and TDS with them. Our output
must be the round figure.

Multiplying first makes the identity hold: when payable days equal the days in
the month, earned pay is exactly the monthly rate. Dividing first does not, and
no amount of downstream rounding repairs it, because rounding a wrong intermediate
produces a confidently wrong invoice.

## Why the per-day rate on the remuneration sheet must never be multiplied back

The remuneration sheet has a column for the rate per day. On a per-month
engagement that column is a display value and nothing else. It is computed
independently of earned pay, and earned pay never references it.

The independence is the entire point. The legacy process rounds the displayed per
day rate and then multiplies by it, and it loses money in both directions
depending on which way the rounding fell. In the shipped samples, one trainer's
row shows a rounded per day rate whose product with the day count is a couple of
rupees above the correct earned figure, another's is under, and a third row has
an arithmetic error where the author wrote the intended answer rather than the
product of the numbers beside it. In one of those rows the sheet's own earned
column and its own per day column disagree with each other.

The rule this produces is general: a rounded value never re-enters a
calculation. Full precision runs through every intermediate, and rounding happens
once, at net.

## Why TDS is charged on earned pay and never on gross

TDS is levied on earned pay. Reimbursements — travel, accommodation, TA and DA —
are the trainer's own money coming back to them and are not income, so they are
not part of the TDS base even though they are part of gross.

Charging TDS on gross instead is a quiet short-payment: the trainer is taxed on
their own travel expenses and receives less than they are owed, and the sheet
looks internally consistent while it happens. The difference is exactly why the
validated sample sheet shows the TDS figure it does rather than the slightly
larger one that a gross-based calculation produces.

The default TDS rate on file is ten percent. It is a rate carried on the payout
input, not a constant compiled into the arithmetic, so an engagement with a
different withholding rate is a data change and not a code change.

## Where rounding happens, and why exactly once

Every intermediate value in a payout is held at full decimal precision. Nothing
is rounded on the way through. The single rounding is applied to net pay, to
whole rupees, at the end.

Money is never held as a floating-point number anywhere in this system, without
exception. The legacy sheet is the argument: it stores earned, gross, TDS and net
for a full month as values that are a fraction of a paisa off the round figure,
and those values were printed on an invoice.

Storage keeps two decimal places and display is whole rupees. The amount in words
on an invoice is generated in Python using Indian numbering — lakh and crore. The
legacy sheet renders an error in that cell because the macro it depends on is
missing, and that is not reproduced.

## Why no money is computed by an agent, and none in SQL

All monetary arithmetic lives in one pure Python module, which opens no
connection, reads no configuration and calls no model. An agent may explain a
payout figure it was handed. It may never produce one. Every number that appears
in a generated message was passed in as structured input, read from a system of
record.

There is equally no arithmetic in the database: no generated column, no sum, no
trigger-computed amount, and no check constraint asserting that gross equals
earned plus the reimbursements. Such a constraint looks like a safety net and is
actually a second implementation of this specification in a language with
different rounding behaviour. It would start rejecting correct rows the first time
the two disagreed at the last paisa.

The validation gates are a separate module from the computation for the same
reason. A payout that should be blocked must compute a blockable number, not a
plausible one, so the engine is never in a position to "fix" bad input.

## How an invoice number is built, and why the system issues it

An invoice number is composed of the first four characters of the trainer's PAN,
the fiscal year, and a three-letter month token with a sequence number. The
fiscal year runs from April to March and is derived from the payout month — not
from today's date and not from the date the invoice was raised. Uniqueness is
enforced by the database on the combination of PAN, fiscal year, month and
sequence.

The system generates invoice numbers and does not accept them. In the legacy
process they are supplied by the trainer on a form whose own placeholder text
shows a fiscal year from a previous year, so people copy it verbatim; roughly one
number in five in the shipped samples is malformed, with the wrong fiscal year, a
four-letter month token, or both.

The consequence has to be expected rather than discovered: during a parallel run,
a regenerated invoice number will sometimes disagree with the historical one. That
is the system being right and the spreadsheet being wrong. Do not resolve it by
echoing back whatever the form captured, which would import the defect and break
the uniqueness rule at the same time.

Trainer identity is PAN throughout. It is the only stable key present in every
legacy remuneration sheet and it seeds the invoice number, so trainers are never
matched by name.

## What blocks a payout cycle

A blocking failure means the cycle cannot be submitted for approval at all. There
are eight, and every one of them is a fact read from a system of record rather
than a judgement.

There must be a signed work order on file, and the payout period must fall
entirely inside its validity dates, both bounds inclusive. A work order that
expired mid-month means the tail of the month needs a fresh work order, not a
wider tolerance. There must be a ZOHO account, without which Finance cannot book
the payment. PAN, bank account number and IFSC must be present and well-formed —
PAN is ten characters in the statutory shape, IFSC is eleven with the reserved
zero in the fifth position, and the bank account must be present and numeric.
There is deliberately no length rule on the account number, because Indian account
numbers vary by bank and any invented length would block valid trainers.

The payable-day count must not exceed the days in the month; on the per-month
branch a count above the month length pays more than the full retainer, and it is
the arithmetic signature of a duplicated attendance row. Attendance must be
complete for the period on a CRT engagement. The invoice number must not already
have been issued. The engagement rate must equal the rate in the signed work
order, compared by value so that a rate written with and without decimal places
still matches. And net pay must be greater than zero — zero is blocked as well as
negative, because a zero net is either a data error or a payout that should not
have been raised, and neither should travel through approval and land as a ₹0
bank instruction.

Every gate runs even after one has failed. The report comes back whole, because a
Manager fixing a payout wants the entire list once rather than one blocker per
round trip.

## What warns, and what a stated reason is for

A warning does not stop a payout cycle. It requires a reason, recorded against the
run, and an empty or whitespace-only reason does not count.

Net pay that deviates by more than twenty percent from the trailing three-month
average warns. The comparison is silent for a trainer with no history, because a
first payout has nothing to deviate from and warning on every new joiner would
teach everyone to ignore the warning.

A reimbursement claimed with zero payable days warns. It is legitimate — travel
booked for a deployment that was then cancelled still cost the trainer money — and
it is also exactly what a TA and DA claim filed against the wrong month looks
like, which is why it needs a sentence from a human rather than a block.

Incomplete attendance on a bCAP engagement warns, for the reason set out in the
attendance document: the failure mode is an over-count that a person can knowingly
accept, rather than an under-count nobody would notice.

## Why the remuneration sheet keeps a misspelled column heading

The remuneration sheet has twenty-one columns and the invoice sheet has
thirty-four populated ones, both in a fixed order taken from the files Finance
already reads. That order is an output contract. One of the headings misspells
"Accommodation", and the misspelling is preserved deliberately. Correcting it
silently breaks every lookup formula written against the file downstream, and
people trust the format they read.

## Two cells that are deliberately left empty

The invoice sheet has a field named "Expense for the month". In the shipped
sample it holds a single date against a full-month payout period, and nobody has
been able to say what it means. It is CLAUDE.md §14 question 2 and it is
unanswered, so the generator leaves the cell empty rather than filling it with a
plausible date. An invented value there would be indistinguishable from a real one
within a month.

The invoice sheet also carries a recipient field labelled "AM Mail ID". Who "AM"
refers to has not been established, so it is left empty and the comms templates
are not wired to it. Both gaps are carried openly rather than closed by guessing.

## Open question: the terms of a CRT engagement

CLAUDE.md §14 question 1 is open and this document does not answer it. Both
regression fixtures behind the payout engine are bCAP, and a validated per-day
remuneration sheet does not exist on file.

What the evidence does establish is that the day rate is written in the work
order: the one per-day trainer in the form responses has a day rate recorded in a
field labelled as being taken from the work order, and the earned figure is the
day rate multiplied by the attended days with no month divisor, over a period
longer than the day count. That confirms the count-up rule for CRT.

What it does not establish is whether TA and DA on a CRT engagement is paid per
travel day. Both per-day rows on file record it as absent, which is evidence in
neither direction. It also does not establish whether a day rate can be set per
program rather than per work order. Do not guess either.

A related data-quality warning, recorded from the same source: in the legacy form
the program type is sometimes written in prose rather than as a type, and a per-day
rate has been entered into a field labelled "per month". Program type and rate
basis cannot be read off those fields naively.

## Open question: whether Finance accepts a generated remuneration sheet

CLAUDE.md §14 question 4 is open. It is not known whether Finance will accept a
system-generated remuneration sheet or whether the sheet must continue to be
prepared manually, and the answer decides whether the payout phase can be
automated end to end. Until it is answered, the generated sheet is treated as an
output for a human to check against the format they already use.

## Who approves a payout, and what approval does

A remuneration sheet is approved by a Senior Manager. That is the only artifact
type with an approval authority defined today, and it comes straight from the
persona definition, which places payout approval with the Senior Manager.

Approval freezes the version and hashes it; release is a separate action, taken
later, by a separate human, with its own audit row. The lifecycle, the freeze and
the reason agents cannot release anything are covered in the approval and release
document.
